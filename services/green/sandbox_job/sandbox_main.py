#!/usr/bin/env python3
"""Entrypoint for the Green Team sandbox-test Cloud Run Job.

Runs as a separate Cloud Run Job, not inside the main green_team service --
on purpose. Applying an LLM-drafted diff and then running whatever test
command that repo defines is arbitrary code execution, and it must not run
inside the container that also holds the Firestore and GitHub credentials
with write access to incident state. If the sandboxed test command turns
out to be malicious or just badly behaved, the blast radius should be "a
disposable job execution," not "the service that holds all the Green
Team's credentials" -- the same reasoning the trigger service already uses
for scoping its own service account (see ../README.md).

Reads its target incident from the one --incident-id container-arg
override the triggering service passes (see ../tools_sandbox.py), and
everything else -- the proposed diff, the repo, the test command -- from
Firestore and env vars, the same split job_main.py already uses for the
red-team job.

Always exits 0 once it has *attempted* the test and recorded a result,
whether that result is pass or fail -- a failing test is an expected,
informative outcome, not a job crash. Only exits non-zero for actual
infra failures (bad incident id, repo unreachable, Firestore unreachable)
where no result could be recorded at all.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from google.cloud import firestore

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # "owner/name"
TEST_COMMAND = os.environ.get("TEST_COMMAND", "")  # e.g. "mvn -q test"
REF = os.environ.get("TEST_REF", "main")


def _db() -> firestore.Client:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    return firestore.Client(project=project) if project else firestore.Client()


def _record_result(db: firestore.Client, incident_id: str, *, passed: bool, stage: str, output: str) -> None:
    doc_ref = db.collection("incidents").document(incident_id)
    doc_ref.set(
        {
            "sandbox_test": {
                "passed": passed,
                "stage": stage,
                "output": output[-4000:],
                "ran_at": time.time(),
            }
        },
        merge=True,
    )
    doc_ref.collection("timeline").add(
        {
            "event": "sandbox_test_passed" if passed else "sandbox_test_failed",
            "detail": f"stage={stage}",
            "actor": "sandbox_job",
            "at": time.time(),
        }
    )


def _run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incident-id", required=True)
    args = parser.parse_args()
    incident_id = args.incident_id

    db = _db()
    incident = db.collection("incidents").document(incident_id).get()
    if not incident.exists:
        print(f"error: incident {incident_id} not found in Firestore", file=sys.stderr)
        return 1
    proposed_fix = incident.to_dict().get("proposed_fix")
    if not proposed_fix:
        print(f"error: incident {incident_id} has no proposed_fix to test", file=sys.stderr)
        return 1

    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("error: GITHUB_TOKEN / GITHUB_REPO not configured", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        clone_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
        clone = _run(
            ["git", "clone", "--depth", "1", "--branch", REF, clone_url, str(repo_dir)],
            cwd=Path(tmpdir),
            timeout=60,
        )
        if clone.returncode != 0:
            print(f"error: git clone failed: {clone.stderr}", file=sys.stderr)
            return 1

        diff_path = Path(tmpdir) / "fix.diff"
        diff_path.write_text(proposed_fix["diff"])

        # Stage 1: does the diff even apply to the real, current file? This
        # alone catches the most common failure mode -- the model citing
        # code that doesn't actually exist in the file it read.
        check = _run(["git", "apply", "--check", str(diff_path)], cwd=repo_dir, timeout=20)
        if check.returncode != 0:
            _record_result(db, incident_id, passed=False, stage="apply", output=check.stderr)
            print("sandbox result: FAILED at apply stage")
            return 0

        apply = _run(["git", "apply", str(diff_path)], cwd=repo_dir, timeout=20)
        if apply.returncode != 0:
            _record_result(db, incident_id, passed=False, stage="apply", output=apply.stderr)
            print("sandbox result: FAILED at apply stage (applied check passed but apply itself failed)")
            return 0

        # Stage 2: run the repo's own test command, if one is configured.
        # Without TEST_COMMAND we can still report "diff applies cleanly"
        # -- partial signal is still signal, not a reason to fail closed.
        if not TEST_COMMAND:
            _record_result(
                db, incident_id, passed=True, stage="apply_only",
                output="Diff applies cleanly. No TEST_COMMAND configured for this sandbox job, so no test suite was run.",
            )
            print("sandbox result: PASSED (apply-only, no TEST_COMMAND configured)")
            return 0

        try:
            tests = _run(TEST_COMMAND.split(), cwd=repo_dir, timeout=300)
        except subprocess.TimeoutExpired:
            _record_result(db, incident_id, passed=False, stage="test", output="Test command timed out after 300s.")
            print("sandbox result: FAILED (test command timed out)")
            return 0

        output = (tests.stdout or "") + "\n" + (tests.stderr or "")
        passed = tests.returncode == 0
        _record_result(db, incident_id, passed=passed, stage="test", output=output)
        print(f"sandbox result: {'PASSED' if passed else 'FAILED'} at test stage")
        return 0


if __name__ == "__main__":
    sys.exit(main())
