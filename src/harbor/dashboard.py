"""Loopback dashboard for the current Harbor supervisor."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from aiohttp import web

from harbor.console import Console
from harbor.digest import render_digest
from harbor.supervisor import SupervisorStatus

ASSETS = Path(__file__).with_name("dashboard_assets")
HOST = "127.0.0.1"


def _snapshot(console: Console) -> SupervisorStatus:
    status = console.supervisor.status()
    # The ledger may hold runs for several projects. Only expose runs attached
    # to tickets from this supervisor's current observation.
    observed = {ticket["identifier"] for ticket in status.tickets}
    return replace(status, runs=tuple(run for run in status.runs if run.get("ticket") in observed))


def _select(row: dict, *keys: str) -> dict:
    return {key: row[key] for key in keys if key in row}


def _dashboard_status(console: Console) -> dict:
    status = _snapshot(console)
    return {
        "project": status.project,
        "lifecycle": status.lifecycle,
        "linear_healthy": status.linear_healthy,
        "github_healthy": status.github_healthy,
        "has_error": bool(status.last_error or status.github_error),
        "observed_at": status.last_activity,
        "read_only": console.supervisor.read_only,
        "tickets": [
            _select(ticket, "identifier", "state", "title", "url", "hold", "owned",
                    "pr_urls", "prs", "ci", "threads", "handoff", "blocked_by")
            for ticket in status.tickets
        ],
        "runs": [
            _select(run, "ticket", "role", "status", "waiting_on", "outcome", "pr_url", "quota_recovery", "next_action")
            for run in status.runs
        ],
        "escalations": [
            _select(row, "id", "ticket", "kind", "question", "reason")
            for row in status.escalations
        ],
    }


def create_app(console: Console, *, port: int, token: str) -> web.Application:
    authority = f"{HOST}:{port}"
    origin = f"http://{authority}"

    def has_launch_token(request: web.Request) -> bool:
        supplied = request.headers.get("Authorization", "")
        return secrets.compare_digest(supplied.encode(), f"Bearer {token}".encode())

    @web.middleware
    async def protect(request: web.Request, handler):
        if request.headers.get("Host") != authority:
            raise web.HTTPForbidden()
        if request.path.startswith("/api/"):
            origin_header = request.headers.get("Origin")
            if origin_header not in (None, origin) or request.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
                raise web.HTTPForbidden()
            status_request = request.path == "/api/status" and request.method in {"GET", "HEAD"}
            chat_request = request.path == "/api/chat" and request.method == "POST"
            if request.method == "POST" and origin_header != origin:
                raise web.HTTPForbidden()
            if not (status_request or chat_request or has_launch_token(request)):
                return web.json_response({"error": "Open the dashboard launch link to record a decision."}, status=401)
        response = await handler(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        })
        return response

    async def asset(request: web.Request) -> web.FileResponse:
        return web.FileResponse(ASSETS / assets[request.path])

    async def status(request: web.Request) -> web.Response:
        return web.json_response({**_dashboard_status(console), "actions_authorized": has_launch_token(request)})

    async def submit(request: web.Request) -> web.Response:
        try:
            if request.content_type != "application/json":
                raise ValueError
            payload = await request.json()
            message = payload["message"].strip()
            if not message or len(message) > 4000:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            return web.json_response({"error": "Enter a message of 1–4,000 characters."}, status=400)

        escalation = request.match_info.get("escalation")
        if escalation is not None:
            supervisor = console.supervisor
            if supervisor.read_only or supervisor.shadow:
                return web.json_response({"error": "This dashboard is read-only."}, status=403)
            if not any(str(row["id"]) == escalation for row in supervisor.status().escalations):
                return web.json_response({"error": "This escalation is no longer open here."}, status=409)
            try:
                receipt = await supervisor.answer_escalation(escalation, message, background=True)
            except (RuntimeError, ValueError) as exc:
                return web.json_response({"error": str(exc)}, status=409)
            reply = f"Answer recorded for escalation #{escalation}."
            if receipt.get("queued"):
                reply += " The retained builder is queued to resume."
        else:
            history = payload.get("history", [])
            if not isinstance(history, list) or len(history) > 6 or any(
                not isinstance(turn, dict)
                or turn.get("role") not in ("You", "First Mate")
                or not isinstance(turn.get("text"), str)
                or len(turn["text"]) > 1200
                for turn in history
            ):
                return web.json_response({"error": "Invalid conversation history."}, status=400)
            question = message
            if history:
                question = (
                    "Use this prior conversation only as context for the latest question. "
                    "Use the current Harbor snapshot for current status.\n"
                    f"Prior conversation: {json.dumps(history, ensure_ascii=False)}\n"
                    f"Latest question: {message}"
                )
            try:
                reply = await console.ask(
                    question, digest=render_digest(_snapshot(console)), interactive=False,
                )
            except Exception as exc:
                return web.json_response({"error": str(exc) or "First Mate could not answer."}, status=503)
        return web.json_response({"reply": reply})

    assets = {"/": "index.html", "/status": "index.html", "/dashboard.css": "dashboard.css", "/dashboard.js": "dashboard.js"}
    app = web.Application(middlewares=[protect], client_max_size=32 * 1024)
    for path in assets:
        app.router.add_get(path, asset)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/chat", submit)
    app.router.add_post(r"/api/escalations/{escalation:\d+}/answer", submit)
    return app


@asynccontextmanager
async def serve_dashboard(console: Console, port: int) -> AsyncIterator[str]:
    token = secrets.token_urlsafe(32)
    runner = web.AppRunner(
        create_app(console, port=port, token=token),
        access_log=None,
        handler_cancellation=True,
        shutdown_timeout=1,
    )
    await runner.setup()
    try:
        await web.TCPSite(runner, HOST, port).start()
        # Fragments stay in the browser and never reach the HTTP server.
        yield f"http://{HOST}:{port}/#{token}"
    finally:
        console.cancel_answer()
        await runner.cleanup()
