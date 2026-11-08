"""Green Team: the ADK coordinator agent, as a public HTTP service.

Three entry points:
  POST /incidents       -- automatic trigger (Cloud Logging alert / Eventarc)
  POST /chat             -- human operator, from the dashboard's "AI assistant" panel
  POST /approvals/<id>   -- the dashboard's "Approve & execute" / "Reject" buttons

Auth model matches services/blue and the red team's trigger service: Cloud Run
IAM stays allow-unauthenticated, every request must carry a bearer token
checked here in app code, read from Secret Manager via env var.
"""
from __future__ import annotations

import asyncio
import os
import uuid

from flask import Flask, jsonify, request
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

import state
from agent import coordinator_agent
from tools_code import open_pull_request

app = Flask(__name__)

GREEN_TEAM_TOKEN = os.environ.get("GREEN_TEAM_TOKEN", "")
APP_NAME = "evolveops-green"

# InMemorySessionService is a deliberate choice, not a shortcut taken and
# forgotten -- see agent.py's module docstring. A session here only needs
# to live for the duration of one coordinator turn (seconds), never across
# the human-approval wait.
_session_service = InMemorySessionService()
_runner = Runner(app_name=APP_NAME, agent=coordinator_agent, session_service=_session_service)


@app.before_request
def check_auth():
    if request.path == "/healthz":
        return None
    auth = request.headers.get("Authorization", "")
    if not GREEN_TEAM_TOKEN or auth != f"Bearer {GREEN_TEAM_TOKEN}":
        return jsonify({"error": "unauthorized"}), 401


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


def _run_coordinator(user_id: str, message: str) -> str:
    """Bridge Flask's sync world to ADK's async Runner for exactly one turn."""

    async def _go() -> str:
        session = await _session_service.create_session(app_name=APP_NAME, user_id=user_id)
        content = types.Content(role="user", parts=[types.Part(text=message)])
        reply_parts: list[str] = []
        async for event in _runner.run_async(
            user_id=user_id, session_id=session.id, new_message=content
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        reply_parts.append(part.text)
        return "\n".join(reply_parts) if reply_parts else "(no reply)"

    return asyncio.run(_go())


@app.route("/incidents", methods=["POST"])
def new_incident():
    """Automatic entry point. A Cloud Logging alert policy calls this via Eventarc."""
    body = request.get_json(force=True)
    incident_id = body.get("incident_id") or f"inc-{uuid.uuid4().hex[:8]}"
    service = body.get("service", "unknown-service")
    title = body.get("title", f"{service} incident")
    state.create_incident(
        incident_id, title=title, service=service, severity=body.get("severity", "warning")
    )

    reply = _run_coordinator(
        user_id="system",
        message=(
            f"New automatic incident alert. incident_id={incident_id}, service={service}. "
            f"Run diagnosis and, if you find a fix, draft it and request approval."
        ),
    )
    return jsonify({"incident_id": incident_id, "coordinator_reply": reply}), 202


@app.route("/chat", methods=["POST"])
def chat():
    """Interactive entry point. The dashboard's AI assistant panel calls this."""
    body = request.get_json(force=True)
    message = body.get("message", "")
    operator = body.get("operator", "dashboard-user")
    if not message:
        return jsonify({"error": "message required"}), 400
    reply = _run_coordinator(user_id=operator, message=message)
    return jsonify({"reply": reply})


@app.route("/approvals/<incident_id>", methods=["POST"])
def approve(incident_id: str):
    """The dashboard's Approve / Reject buttons call this directly -- no LLM call."""
    body = request.get_json(force=True)
    decision = body.get("decision")
    approver = body.get("approver", "unknown")
    if decision not in ("approve", "reject"):
        return jsonify({"error": "decision must be 'approve' or 'reject'"}), 400

    if decision == "reject":
        state.update_incident(incident_id, status="rejected")
        state.append_timeline(incident_id, event="rejected", actor=approver)
        return jsonify({"status": "rejected"})

    state.append_timeline(incident_id, event="approved", actor=approver)
    try:
        pr = open_pull_request(incident_id)
    except Exception as exc:
        state.update_incident(incident_id, status="execution_failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"status": "executing", "pr_url": pr.get("html_url")})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
