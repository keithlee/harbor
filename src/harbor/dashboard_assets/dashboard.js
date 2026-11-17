"use strict";

const byId = (name) => document.getElementById(name);
const token = location.hash.slice(1) || sessionStorage.getItem("harbor-dashboard-token") || "";
if (location.hash) {
  sessionStorage.setItem("harbor-dashboard-token", token);
  history.replaceState(null, "", location.pathname);
}

let snapshot;
let connected = false;
let selectedDecision = null;
let asking = false;
let answering = false;
const transcript = [];

function node(tag, content, className) {
  const element = document.createElement(tag);
  if (content != null) element.textContent = content;
  if (className) element.className = className;
  return element;
}

function link(label, href, className) {
  const element = node("span", label, className);
  if (typeof href !== "string") return element;
  try {
    const url = new URL(href);
    if (url.protocol !== "https:") return element;
    const anchor = node("a", label, className);
    anchor.href = url.href;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    return anchor;
  } catch {
    return element;
  }
}

async function api(path, payload) {
  const headers = token ? { Authorization: "Bearer " + token } : {};
  const options = { headers, cache: "no-store", credentials: "omit" };
  if (payload !== undefined) {
    headers["Content-Type"] = "application/json";
    options.method = "POST";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Harbor could not complete the request.");
  return data;
}

function setFeedback(id, message, error = false) {
  const feedback = byId(id);
  feedback.textContent = message;
  feedback.hidden = !message;
  feedback.classList.toggle("error", error);
}

function updateButtons() {
  byId("chat-form").querySelector('[type="submit"]').disabled = !connected || asking;
  byId("question").disabled = !connected;
  for (const button of document.querySelectorAll(".suggestions button")) button.disabled = !connected;
  byId("clear-chat").disabled = asking;
  byId("decision-form").querySelector('[type="submit"]').disabled =
    !connected || !snapshot?.actions_authorized || !selectedDecision || snapshot.read_only || answering;
  byId("cancel-decision").disabled = answering;
}

function selectDecision(decision) {
  selectedDecision = decision;
  byId("decision-answer").value = "";
  byId("decision-form").hidden = !decision;
  if (decision) {
    setFeedback("decision-feedback", "");
    byId("decision-label").textContent = "Your decision for " + (decision.ticket || "#" + decision.id);
    byId("decision-answer").focus();
  }
  updateButtons();
}

function renderDecisions() {
  const target = byId("decisions");
  const renderKey = JSON.stringify([snapshot.read_only, snapshot.actions_authorized, snapshot.escalations]);
  if (target.dataset.renderKey === renderKey) return;
  target.dataset.renderKey = renderKey;
  if (selectedDecision && !snapshot.escalations.some((row) => row.id === selectedDecision.id)) {
    selectDecision(null);
  }
  if (!snapshot.escalations.length) {
    target.replaceChildren(node("p", "No open decisions.", "empty"));
    return;
  }
  target.replaceChildren(...snapshot.escalations.map((decision) => {
    const row = node("article", null, "decision");
    const detail = node("div");
    detail.append(node("strong", [decision.ticket, "#" + decision.id].filter(Boolean).join(" · ")));
    detail.append(node("p", decision.question || "Operator input requested."));
    if (decision.reason) detail.append(node("p", decision.reason, "muted"));
    row.append(detail);
    if (!snapshot.read_only && snapshot.actions_authorized) {
      const button = node("button", "Answer", "secondary");
      button.type = "button";
      button.setAttribute("aria-label", "Answer escalation " + decision.id);
      button.addEventListener("click", () => { if (!answering) selectDecision(decision); });
      row.append(button);
    }
    return row;
  }));
}

function pullRequests(ticket, runs) {
  const urls = new Set(ticket.pr_urls || []);
  for (const pr of ticket.prs || []) {
    if (typeof pr === "string") urls.add(pr);
    else if (pr.url || pr.html_url) urls.add(pr.url || pr.html_url);
  }
  for (const run of runs) if (run.pr_url) urls.add(run.pr_url);
  return [...urls];
}

function renderTickets() {
  if (!snapshot) return;
  const filter = byId("filter").value;
  const runsByTicket = new Map();
  for (const run of snapshot.runs) {
    if (!runsByTicket.has(run.ticket)) runsByTicket.set(run.ticket, []);
    runsByTicket.get(run.ticket).push(run);
  }
  const ticketRows = [];
  for (const ticket of snapshot.tickets) {
    const runs = runsByTicket.get(ticket.identifier) || [];
    const builders = runs.filter((run) => run.role === "builder");
    const running = builders.some((run) => run.status === "running");
    const review = /review/i.test(ticket.state);
    const attention = ticket.hold || ticket.blocked_by?.length || ticket.handoff?.length ||
      /blocked|human review/i.test(ticket.state) ||
      snapshot.escalations.some((row) => row.ticket === ticket.identifier) ||
      builders.some((run) => ["blocked", "failed", "orphaned", "terminated"].includes(run.status));
    if ((filter === "working" && !running) || (filter === "review" && !review) ||
        (filter === "attention" && !attention)) continue;

    const row = node("tr");
    const description = node("td");
    description.append(link(ticket.identifier, ticket.url, "ticket-id"), node("span", ticket.title, "ticket-title"));
    const state = node("td");
    state.append(node("span", ticket.state, "pill" + (attention ? " warn" : running ? " active" : "")));
    const activity = node("td");
    const facts = node("div", null, "activity");
    for (const run of builders) {
      let text = "Builder " + run.status;
      if (run.waiting_on && run.waiting_on !== "none") text += " · waiting on " + run.waiting_on;
      if (run.quota_recovery) {
        const { provider, next_retry, confirmed } = run.quota_recovery;
        const when = new Date(next_retry * 1000).toLocaleString();
        text = provider + " usage limit · " + (confirmed ? "resets " : "estimated retry ") + when;
      }
      facts.append(node("span", text));
      if (run.next_action && ["waiting", "blocked"].includes(run.status)) {
        facts.append(node("span", "Next step: " + run.next_action));
      }
    }
    for (const url of pullRequests(ticket, runs)) {
      const number = String(url).match(/\/pull\/(\d+)(?:\b|$)/)?.[1];
      facts.append(link(number ? "PR #" + number : "Pull request", url));
    }
    const ci = typeof ticket.ci === "object" ? ticket.ci?.state : ticket.ci;
    if (ci) facts.append(node("span", "CI " + ci));
    const openThreads = (ticket.threads || []).filter((thread) => !thread.resolved && !thread.isResolved).length;
    if (openThreads) facts.append(node("span", openThreads + " open review " + (openThreads === 1 ? "thread" : "threads")));
    if (ticket.hold) facts.append(node("span", "On hold"));
    if (ticket.blocked_by?.length) facts.append(node("span", "Blocked by " + ticket.blocked_by.join(", ")));
    for (const gate of ticket.handoff || []) if (gate.summary) facts.append(node("span", gate.summary));
    if (!facts.childElementCount) facts.append(node("span", ticket.owned ? "Under Harbor supervision" : "Observed", "muted"));
    activity.append(facts);
    row.append(description, state, activity);
    ticketRows.push(row);
  }
  if (!ticketRows.length) {
    const row = node("tr");
    const cell = node("td", filter === "all" ? "No tickets in the configured poll states." : "No tickets match this filter.", "muted");
    cell.colSpan = 3;
    row.append(cell);
    ticketRows.push(row);
  }
  byId("tickets").replaceChildren(...ticketRows);
}

async function refresh() {
  const connection = byId("connection");
  try {
    const current = await api("/api/status");
    const changed = JSON.stringify(current) !== JSON.stringify(snapshot);
    snapshot = current;
    connected = true;
    connection.classList.toggle("online", current.linear_healthy && current.github_healthy !== false);
    const services = ["Linear " + (current.linear_healthy ? "healthy" : "unavailable")];
    if (current.github_healthy != null) services.push("GitHub " + (current.github_healthy ? "healthy" : "unavailable"));
    const state = current.lifecycle.charAt(0) + current.lifecycle.slice(1).toLowerCase();
    connection.textContent = [state, ...services].join(" · ");
    byId("project").textContent = current.project;
    document.title = current.project + " — Harbor";
    byId("updated").textContent = current.observed_at
      ? "Last activity " + new Date(current.observed_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
      : "";
    byId("notice").hidden = !current.read_only && !current.has_error;
    byId("notice").textContent = current.read_only ? "Read-only observer. Start Harbor with the project lease to record decisions."
      : "Harbor reported an operational error. See /status in the terminal for details.";
    byId("actions-notice").hidden = current.actions_authorized || !current.escalations.length;
    byId("ticket-count").textContent = current.tickets.length;
    byId("running-count").textContent = current.runs.filter((run) => run.role === "builder" && run.status === "running").length;
    byId("decision-count").textContent = current.escalations.length;
    if (changed) {
      renderTickets();
      renderDecisions();
    }
  } catch (error) {
    connected = false;
    connection.classList.remove("online");
    connection.textContent = "Disconnected";
    byId("notice").hidden = false;
    byId("notice").textContent = snapshot ? "Harbor is disconnected. Showing the last received snapshot."
      : "Could not reach Harbor. Check the supervisor terminal.";
  }
  updateButtons();
}

function showMessage(role, text) {
  const container = byId("messages");
  container.querySelector(".welcome").hidden = true;
  const message = node("article", null, "message" + (role === "You" ? " user" : ""));
  message.append(node("strong", role), node("p", text));
  container.append(message);
  container.scrollTop = container.scrollHeight;
}

byId("filter").addEventListener("change", renderTickets);
byId("cancel-decision").addEventListener("click", () => selectDecision(null));
byId("clear-chat").addEventListener("click", () => {
  transcript.length = 0;
  byId("messages").querySelectorAll(".message").forEach((message) => message.remove());
  byId("messages").querySelector(".welcome").hidden = false;
  setFeedback("chat-feedback", "");
});
for (const button of document.querySelectorAll(".suggestions button")) {
  button.addEventListener("click", () => {
    byId("question").value = button.textContent;
    byId("question").focus();
  });
}
byId("question").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !event.metaKey || event.shiftKey || event.altKey ||
      event.ctrlKey || event.isComposing) return;
  event.preventDefault();
  if (!event.repeat && connected && !asking) byId("chat-form").requestSubmit();
});
byId("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = byId("question");
  const message = input.value.trim();
  if (!message || !connected || asking) return;
  asking = true;
  updateButtons();
  input.value = "";
  setFeedback("chat-feedback", "First Mate is thinking…");
  try {
    showMessage("You", message);
    const data = await api("/api/chat", {
      message, history: transcript.slice(-6).map((turn) => ({ ...turn, text: turn.text.slice(0, 1200) })),
    });
    showMessage("First Mate", data.reply);
    transcript.push({ role: "You", text: message }, { role: "First Mate", text: data.reply });
    setFeedback("chat-feedback", "");
  } catch (error) {
    if (!input.value) input.value = message;
    setFeedback("chat-feedback", error.message, true);
  } finally {
    asking = false;
    updateButtons();
  }
});
byId("decision-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = byId("decision-answer").value.trim();
  if (!selectedDecision || !message || !connected || !snapshot?.actions_authorized || snapshot.read_only || answering) return;
  answering = true;
  updateButtons();
  setFeedback("decision-feedback", "Recording decision…");
  try {
    const data = await api("/api/escalations/" + encodeURIComponent(selectedDecision.id) + "/answer", { message });
    selectDecision(null);
    setFeedback("decision-feedback", data.reply);
    await refresh();
  } catch (error) {
    setFeedback("decision-feedback", error.message, true);
  } finally {
    answering = false;
    updateButtons();
  }
});
updateButtons();
refresh();
setInterval(refresh, 5000);
