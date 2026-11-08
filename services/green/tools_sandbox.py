"""Tool for the Mitigation agent: run the proposed fix in an isolated
sandbox and report whether it actually applies and passes tests.

The sandbox itself is a separate Cloud Run Job (`sandbox_job/`), triggered
the same way the red team's `deploy/trigger_service/app.py` starts its job --
`run_v2.JobsClient().run_job()` with a container-arg override, then poll
`ExecutionsClient().get_execution()` until it completes. See
`sandbox_job/sandbox_main.py` for why this runs as a separate job rather
than a subprocess in this service.

Note on the `operation.metadata.name` trick for getting the execution name
without waiting for the job to finish: this is the same pattern
the red team's trigger service uses, with the same caveat
flagged in services/red/DEPLOY.md -- if the installed `google-cloud-run` client
version returns something slightly different, this will raise instead of
silently misbehaving, and the real exception ends up in the tool's return
string for the agent (and whoever's debugging) to see.
"""
from __future__ import annotations

import os
import time

from google.cloud import run_v2

import state

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
REGION = os.environ.get("REGION", "asia-east1")
SANDBOX_JOB_NAME = os.environ.get("SANDBOX_JOB_NAME", "evolveops-green-sandbox")
SANDBOX_JOB_PATH = f"projects/{PROJECT_ID}/locations/{REGION}/jobs/{SANDBOX_JOB_NAME}"

POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 90


def run_sandbox_test(incident_id: str) -> str:
    """Apply the incident's proposed fix in an isolated sandbox and run its tests.

    Must be called after `propose_patch` has recorded a `proposed_fix` for
    this incident. Blocks for up to ~90 seconds waiting for the sandbox
    job to finish -- that is the budget available within one agent turn,
    not a guarantee every test suite fits in it.

    Args:
        incident_id: the incident whose proposed_fix should be tested.

    Returns:
        "Sandbox test PASSED/FAILED (stage: apply|test)" plus a tail of
        the output, so you can see *why* it failed and revise the diff
        accordingly. If the job doesn't finish in time, returns a message
        saying the result is inconclusive -- treat that as "unknown,"
        never as a pass.
    """
    if not PROJECT_ID:
        return "error: GOOGLE_CLOUD_PROJECT not configured, cannot start sandbox job"

    jobs = run_v2.JobsClient()
    req = run_v2.RunJobRequest(
        name=SANDBOX_JOB_PATH,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    args=["--incident-id", incident_id],
                )
            ]
        ),
    )
    try:
        operation = jobs.run_job(request=req)
        execution_name = operation.metadata.name
    except Exception as exc:
        return f"error: failed to start sandbox job: {exc}"

    state.append_timeline(incident_id, event="sandbox_test_started", detail=execution_name)

    executions = run_v2.ExecutionsClient()
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    execution = None
    while time.time() < deadline:
        execution = executions.get_execution(name=execution_name)
        if execution.completion_time:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    if execution is None or not execution.completion_time:
        return (
            "Sandbox job did not finish within 90 seconds -- result is "
            "inconclusive, do not treat this as a pass. You may call "
            "run_sandbox_test again to check on it, or proceed to "
            "request_human_approval and note that sandbox results are "
            "still pending."
        )

    incident = state.get_incident(incident_id)
    result = (incident or {}).get("sandbox_test")
    if not result:
        return "error: sandbox job finished but recorded no result -- check its own Cloud Logging output"

    status = "PASSED" if result.get("passed") else "FAILED"
    return f"Sandbox test {status} (stage: {result.get('stage')}).\n{result.get('output', '')[:1500]}"
