#!/usr/bin/env python3
"""Entrypoint for the red-team Cloud Run Job.

Reads its parameters from the container-arg overrides the trigger service
passes (--run-id/--mode/--max-turns) and its connection details from env
vars (TARGET_URL, TARGET_TOKEN, RESULTS_BUCKET, ANTHROPIC_API_KEY).
Runs one campaign against the deployed target service over HTTPS,
then uploads the findings JSONL, HTML report, and patch proposals to a GCS
bucket so they survive the job exiting (Cloud Run Jobs have no persistent
local disk between executions).
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app/vendor")

from redloop.attacks import load_seeds
from redloop.attacker import Attacker
from redloop.feedback import build_patch_proposals, render_proposals_markdown
from redloop.gcp_logging import get_logger, log
from redloop.judge import Judge
from redloop.llm import get_attacker_backend, get_backend
from redloop.orchestrator import Orchestrator
from redloop.report import build_report
from redloop.store import Store
from redloop.target import HTTPTarget

_logger = get_logger("job_main")


def upload_to_gcs(bucket_name: str, local_path: Path, dest_path: str) -> None:
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    bucket.blob(dest_path).upload_from_filename(str(local_path))
    log(_logger, "INFO", "uploaded_result", event="uploaded_result", bucket=bucket_name, path=dest_path)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", default=None)
    p.add_argument("--mode", default="manual")
    p.add_argument("--max-turns", type=int, default=3)
    args = p.parse_args()

    target_url = os.environ["TARGET_URL"]
    target_token = os.environ.get("TARGET_TOKEN", "")
    bucket_name = os.environ.get("RESULTS_BUCKET")

    log(_logger, "INFO", "job_started", event="job_started", target=target_url,
        requested_run_id=args.run_id, mode=args.mode, max_turns=args.max_turns)

    backend = get_backend()
    # TARGET_URL is the target's base URL; HTTPTarget appends /login and /chat.
    target = HTTPTarget(base_url=target_url,
                        user_id=os.environ.get("TARGET_USER_ID", ""),
                        password=os.environ.get("TARGET_PASSWORD", ""),
                        token=target_token)
    attacker = Attacker(get_attacker_backend())
    judge = Judge(backend)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        store = Store(tmp / "findings.jsonl")
        orch = Orchestrator(target, attacker, judge, store, max_turns=args.max_turns)
        seeds = load_seeds("/app/seeds/seeds.yaml")
        run_id = orch.run_campaign(seeds, run_id=args.run_id)

        all_findings = store.read_all()
        run_findings = [f for f in all_findings if f["run_id"] == run_id]

        report_path = build_report({run_id: run_findings}, tmp / "report.html",
                                    title=f"RedLoop Report — {run_id}")

        proposals = build_patch_proposals(run_findings, min_severity=2)
        proposals_path = tmp / "proposals.md"
        proposals_path.write_text(render_proposals_markdown(proposals))

        if bucket_name:
            prefix = f"runs/{run_id}"
            upload_to_gcs(bucket_name, tmp / "findings.jsonl", f"{prefix}/findings.jsonl")
            upload_to_gcs(bucket_name, report_path, f"{prefix}/report.html")
            upload_to_gcs(bucket_name, proposals_path, f"{prefix}/proposals.md")
        else:
            log(_logger, "WARNING", "no_results_bucket_configured",
                event="no_results_bucket_configured", run_id=run_id)

    log(_logger, "INFO", "job_finished", event="job_finished", run_id=run_id)


if __name__ == "__main__":
    main()
