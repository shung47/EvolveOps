"""Trigger service: public HTTPS entry point that starts a red-team
campaign by launching a Cloud Run Job execution.

POST /trigger  {"run_id": "...", "mode": "baseline"|"post-patch", "max_turns": 3}
  -> 202 {"execution": "projects/.../locations/.../jobs/.../executions/..."}

GET /status/<execution_id>
  -> {"state": "RUNNING"|"SUCCEEDED"|"FAILED", "succeeded": n, "failed": n}

Auth: a static shared secret in the X-Trigger-Key header, checked against
Secret Manager-sourced env var TRIGGER_KEY. This is what makes "hit an
HTTPS endpoint" work for any caller, not just ones with GCP credentials --
the trigger service itself holds the one GCP permission needed
(run.jobs.run on this specific job) via its own service account.
"""
from __future__ import annotations

import os
import uuid

from flask import Flask, jsonify, request
from google.cloud import run_v2

app = Flask(__name__)

PROJECT_ID = os.environ["PROJECT_ID"]
REGION = os.environ["REGION"]
JOB_NAME = os.environ.get("JOB_NAME", "evolveops-red-job")
TRIGGER_KEY = os.environ.get("TRIGGER_KEY", "")

JOB_PATH = f"projects/{PROJECT_ID}/locations/{REGION}/jobs/{JOB_NAME}"


def _check_auth():
    key = request.headers.get("X-Trigger-Key", "")
    if not TRIGGER_KEY or key != TRIGGER_KEY:
        return False
    return True


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/trigger", methods=["POST"])
def trigger():
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    run_id = body.get("run_id") or f"triggered-{uuid.uuid4().hex[:8]}"
    mode = body.get("mode", "baseline")
    max_turns = str(body.get("max_turns", 3))

    client = run_v2.JobsClient()
    req = run_v2.RunJobRequest(
        name=JOB_PATH,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    args=["--run-id", run_id, "--mode", mode, "--max-turns", max_turns],
                )
            ]
        ),
    )
    try:
        operation = client.run_job(request=req)
        execution_name = operation.metadata.name
    except Exception as exc:  # noqa: BLE001 -- surface the real GCP error to the caller
        return jsonify({"error": "failed_to_start_job", "detail": str(exc)}), 500

    return jsonify({"execution": execution_name, "run_id": run_id, "mode": mode}), 202


@app.route("/status/<path:execution_id>")
def status(execution_id: str):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    name = execution_id if execution_id.startswith("projects/") else f"{JOB_PATH}/executions/{execution_id}"
    client = run_v2.ExecutionsClient()
    try:
        execution = client.get_execution(name=name)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": "not_found", "detail": str(exc)}), 404

    if execution.completion_time:
        state = "SUCCEEDED" if execution.failed_count == 0 else "FAILED"
    else:
        state = "RUNNING"

    return jsonify({
        "state": state,
        "running": execution.running_count,
        "succeeded": execution.succeeded_count,
        "failed": execution.failed_count,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
