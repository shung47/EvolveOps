"""Firestore-backed incident state for the Green Team.

This is the durable record the operator dashboard reads and writes, and the
thing that survives a Cloud Run instance recycling while an incident sits
in "awaiting approval" for an unknown amount of time -- see the note in
agent.py's module docstring for why we deliberately do NOT try to keep an
ADK session suspended across that wait, and use this Firestore record as
the handoff point instead.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from google.cloud import firestore

INCIDENTS = "incidents"

_db: Optional[firestore.Client] = None


def db() -> firestore.Client:
    global _db
    if _db is None:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        _db = firestore.Client(project=project) if project else firestore.Client()
    return _db


def create_incident(incident_id: str, **fields: Any) -> dict:
    doc = {
        "status": "investigating",
        "created_at": time.time(),
        "updated_at": time.time(),
        **fields,
    }
    db().collection(INCIDENTS).document(incident_id).set(doc)
    append_timeline(incident_id, event="incident_created", detail=fields.get("title", incident_id))
    return doc


def get_incident(incident_id: str) -> Optional[dict]:
    """Read one incident's current record directly from Firestore.

    Args:
        incident_id: the incident to look up.

    Returns:
        The incident document as a dict -- title, service, severity,
        status, root_cause (once diagnostic_agent has run), proposed_fix
        (once propose_patch has run), sandbox_test (once run_sandbox_test
        has run), verification (once verifier_agent has scored it) -- or
        None if the incident doesn't exist. Used by verifier_agent to read
        the ground truth itself rather than trusting a relayed summary.
    """
    snap = db().collection(INCIDENTS).document(incident_id).get()
    return snap.to_dict() if snap.exists else None


def update_incident(incident_id: str, **fields: Any) -> None:
    fields["updated_at"] = time.time()
    db().collection(INCIDENTS).document(incident_id).set(fields, merge=True)


def append_timeline(incident_id: str, *, event: str, detail: str = "", actor: str = "system") -> None:
    db().collection(INCIDENTS).document(incident_id).collection("timeline").add(
        {"event": event, "detail": detail, "actor": actor, "at": time.time()}
    )


def get_timeline(incident_id: str) -> list[dict]:
    """Return one incident's full timeline, oldest first.

    Args:
        incident_id: the incident to look up.

    Returns:
        A list of {event, detail, actor, at} dicts. Used by the Reporter
        agent to write the post-mortem, and by the dashboard's "activity timeline"
        panel.
    """
    docs = (
        db()
        .collection(INCIDENTS)
        .document(incident_id)
        .collection("timeline")
        .order_by("at")
        .stream()
    )
    return [d.to_dict() for d in docs]
