"""Ephemeral Playwright browsers for sandboxed builders."""

from __future__ import annotations

import asyncio
import re
import secrets
from contextlib import asynccontextmanager, suppress


ENDPOINT_ENV = "HARBOR_PLAYWRIGHT_WS_ENDPOINT"
REQUIRED_ENV = "HARBOR_PLAYWRIGHT_REQUIRED"
_IMAGE_REF = re.compile(r"(?:[a-zA-Z0-9][a-zA-Z0-9./:_-]*@)?sha256:[a-f0-9]{64}")


def validate_image(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not _IMAGE_REF.fullmatch(value)):
        raise ValueError("playwright_browser_image must be a digest-pinned Docker image")


class BrowserUnavailable(RuntimeError):
    pass


async def _docker(*args: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise BrowserUnavailable("Docker CLI unavailable") from exc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (TimeoutError, asyncio.CancelledError):
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        remedies = {
            "info": "Docker is unavailable (start the local Docker runtime)",
            "image": "the configured Playwright image is missing (provision it in local Docker)",
            "run": "Docker could not start the configured Playwright image",
            "port": "the Playwright container exited before publishing its port",
            "rm": "Docker could not remove the Playwright container",
        }
        raise BrowserUnavailable(remedies[args[0]])
    return stdout.decode().strip()


async def _wait_until_listening(port: int, ws_path: str) -> None:
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    async with asyncio.timeout(1):
                        reader, writer = await asyncio.open_connection("127.0.0.1", port)
                        try:
                            writer.write(b"GET /json HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                            await writer.drain()
                            reply = await reader.read()
                            if ws_path.encode() in reply:
                                return
                        finally:
                            writer.close()
                            with suppress(OSError):
                                await writer.wait_closed()
                except (OSError, TimeoutError):
                    pass
                await asyncio.sleep(0.2)
    except TimeoutError as exc:
        raise BrowserUnavailable("the Playwright server did not start listening") from exc


@asynccontextmanager
async def browser_endpoint(image: str, *, lifetime_seconds: int):
    """Yield a loopback URL or an actionable error without exposing Docker output."""
    name = f"harbor-playwright-{secrets.token_hex(12)}"
    ws_path = f"/{secrets.token_urlsafe(24)}"
    endpoint = ""
    reason = ""
    started = False
    try:
        try:
            await _docker("info", "--format", "{{.ServerVersion}}")
            await _docker("image", "inspect", "--format", "{{.Id}}", image)
            # Mark before launching so cancellation after Docker accepts the
            # request also takes the cleanup path. The in-container timeout
            # bounds the lifetime even if the supervising process is killed.
            started = True
            await _docker(
                "run", "--detach", "--rm", "--init", "--pull=never",
                "--name", name, "--publish", "127.0.0.1::3000",
                "--user", "pwuser", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--shm-size", "512m",
                "--entrypoint", "/usr/bin/timeout", image,
                "--kill-after=5s", f"{lifetime_seconds}s", "playwright",
                "run-server", "--host", "0.0.0.0", "--port", "3000", "--path", ws_path,
            )
            binding = await _docker("port", name, "3000/tcp")
            match = re.fullmatch(r"127\.0\.0\.1:(\d+)", binding)
            if match is None or not 0 < int(match[1]) < 65536:
                raise BrowserUnavailable("Playwright did not publish a loopback-only port")
            port = int(match[1])
            await _wait_until_listening(port, ws_path)
            endpoint = f"ws://127.0.0.1:{port}{ws_path}"
        except BrowserUnavailable as exc:
            reason = str(exc)
        except TimeoutError:
            reason = "the Docker command timed out"
        yield endpoint, reason
    finally:
        if started:
            with suppress(BrowserUnavailable, TimeoutError):
                await _docker("rm", "--force", name)
