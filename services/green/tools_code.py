"""Tools for the Mitigation agent: read source, draft a fix, ask approval --
plus the mechanical "actually open the PR" step that runs after approval,
outside the agent entirely.

See agent.py's module docstring for why `request_human_approval` does not
use ADK's `LongRunningFunctionTool`.
"""
from __future__ import annotations

import os

import requests

import state

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # "owner/name"
GITHUB_API = "https://api.github.com"


def read_repo_file(path: str, ref: str = "main") -> str:
    """Read one file's current contents from the project's GitHub repo.

    Args:
        path: repo-relative file path, e.g. "src/main/java/com/example/PaymentController.java".
        ref: branch, tag, or commit SHA to read from. Defaults to "main".

    Returns:
        The file's raw text content, or an "error: ..." string if the file
        or repo isn't reachable -- returned as text rather than raised, so
        the agent sees the failure and can decide what to do next instead
        of the whole turn crashing.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return "error: GITHUB_TOKEN / GITHUB_REPO not configured"
    resp = requests.get(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}",
        params={"ref": ref},
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.raw+json",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        return f"error: GitHub returned {resp.status_code} for {path}"
    return resp.text


def propose_patch(incident_id: str, file_path: str, diff: str, risk: str, rollback: str) -> str:
    """Record a drafted fix for one incident. Does not apply or ship anything.

    Args:
        incident_id: the incident this fix addresses.
        file_path: the repo-relative path the diff applies to.
        diff: a unified diff of the proposed change.
        risk: one short sentence on the risk level and why.
        rollback: one short sentence on how to roll back if it goes wrong.

    Returns:
        Confirmation text.
    """
    state.update_incident(
        incident_id,
        proposed_fix={"file_path": file_path, "diff": diff, "risk": risk, "rollback": rollback},
    )
    state.append_timeline(incident_id, event="fix_drafted", detail=f"{file_path}: {risk}")
    return "Patch proposal recorded for incident " + incident_id


def request_human_approval(incident_id: str) -> str:
    """Mark an incident as awaiting human approval, then stop.

    This does not block, poll, or wait for a response -- it flips the
    incident to `awaiting_approval` in Firestore (which is what makes it
    show up as "awaiting approval" in the dashboard's incident list, and is what the
    Slack notifier watches for) and returns immediately. The agent turn
    ends here.

    Execution resumes, if at all, entirely outside this conversation: a
    human clicks "Approve & execute" in the dashboard, which calls
    `POST /approvals/<incident_id>` on this service directly -- see
    `app.py` and `open_pull_request` below.

    Args:
        incident_id: the incident awaiting approval. Must already have a
            `proposed_fix` recorded via `propose_patch`.

    Returns:
        Confirmation text, or an "error: ..." string if called before a
        patch was proposed.
    """
    incident = state.get_incident(incident_id)
    if not incident or "proposed_fix" not in incident:
        return "error: no proposed_fix recorded for this incident yet -- call propose_patch first"
    state.update_incident(incident_id, status="awaiting_approval")
    state.append_timeline(incident_id, event="approval_requested", detail="Notified via Slack")
    return "Marked awaiting_approval and notified Slack. Stopping here until a human approves."


def open_pull_request(incident_id: str) -> dict:
    """Open a GitHub PR for an already-approved, already-drafted fix.

    Called directly from app.py's `/approvals/<id>` route -- deliberately
    NOT exposed as an agent tool. By the time a human has approved an
    already-drafted diff there's no more reasoning left to do, just
    mechanical execution, so this skips the LLM entirely.

    NOT YET IMPLEMENTED: committing `proposed_fix["diff"]` onto a new
    branch before this call. Doing that correctly through the GitHub REST
    API (rather than a real `git` checkout) needs the Contents API's
    base64 update-file call per changed file, which only works cleanly for
    single-file diffs. A repo clone + `git apply` + push, run as a Cloud
    Build or Cloud Run Job step, is the right answer for multi-file diffs
    -- swap it in here (before the `pulls` POST below) once a real
    multi-file fix shows up; flagging this rather than shipping a
    single-file-only version that would silently misbehave on anything
    bigger.

    Args:
        incident_id: the approved incident.

    Returns:
        The GitHub API's PR object (includes "html_url").

    Raises:
        ValueError: if no proposed_fix exists for this incident.
        requests.HTTPError: if the GitHub API call fails.
    """
    incident = state.get_incident(incident_id)
    if not incident or "proposed_fix" not in incident:
        raise ValueError(f"{incident_id}: no proposed_fix to open a PR for")
    fix = incident["proposed_fix"]
    branch = f"evolveops/{incident_id}"

    resp = requests.post(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "title": f"fix({incident_id}): {fix['risk']}",
            "head": branch,
            "base": "main",
            "body": (
                f"Auto-drafted by the EvolveOps green team (multi-agent SRE).\n\n"
                f"**Risk:** {fix['risk']}\n**Rollback:** {fix['rollback']}\n\n"
                f"```diff\n{fix['diff']}\n```"
            ),
        },
        timeout=15,
    )
    resp.raise_for_status()
    pr = resp.json()
    state.update_incident(incident_id, status="executing", pr_url=pr["html_url"])
    state.append_timeline(incident_id, event="pr_opened", detail=pr["html_url"])
    return pr
