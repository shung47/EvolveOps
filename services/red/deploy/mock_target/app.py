"""Mock target: the ops agent under test, as a public HTTP service.

Wraps the exact same MockOpsAgent logic already validated locally (see
redloop/target.py) behind a Flask app, so what gets attacked over the
internet is the identical, deliberately-partial-guardrail agent from the
offline demo -- not a different reimplementation.

Auth model: Cloud Run IAM is left allow-unauthenticated (the traffic itself
is real HTTP, not gated at the platform layer, per the "attack over the
public internet" design decision) but every request must carry a bearer
token checked here in app code, read from Secret Manager via env var.
"""
from __future__ import annotations

import os
import sys

from flask import Flask, jsonify, request

sys.path.insert(0, "/app/vendor")
from redloop.llm import ChatMessage
from redloop.target import MockOpsAgent

app = Flask(__name__)
target = MockOpsAgent()
TARGET_TOKEN = os.environ.get("TARGET_TOKEN", "")


@app.before_request
def check_auth():
    if request.path == "/healthz":
        return None
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {TARGET_TOKEN}"
    if not TARGET_TOKEN or auth != expected:
        return jsonify({"error": "unauthorized"}), 401


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/chat", methods=["POST"])
def chat():
    body = request.get_json(force=True)
    history = [ChatMessage(role=m["role"], content=m["content"]) for m in body.get("messages", [])]
    if not history:
        return jsonify({"error": "messages required"}), 400
    reply = target.respond(history)
    return jsonify({"reply": reply})


# NOTE: apply_patch is intentionally NOT exposed yet -- applying a guardrail
# patch to a live agent should sit behind human review (see project design
# doc, "Open items"). Wire a POST /admin/patch endpoint here once that
# review workflow exists, protected by a separate admin credential.


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
