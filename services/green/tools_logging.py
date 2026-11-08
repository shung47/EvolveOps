"""Tool for the Diagnostic agent: read Cloud Logging for one incident.

Deliberately returns a de-duplicated, capped summary rather than raw log
entries -- a service logging the same stack trace 500 times a minute would
otherwise blow the token budget for no extra signal. This is the "filter
before it ever reaches the LLM" cost control from the architecture
discussion, not an afterthought.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from google.cloud import logging as cloud_logging

_client: cloud_logging.Client | None = None


def _logging_client() -> cloud_logging.Client:
    global _client
    if _client is None:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        _client = cloud_logging.Client(project=project) if project else cloud_logging.Client()
    return _client


def fetch_incident_logs(service: str, minutes: int = 15, max_entries: int = 50) -> str:
    """Fetch and summarize recent WARNING+ log entries for one Cloud Run service.

    Args:
        service: the Cloud Run service name the incident is about, e.g. "payment-service".
        minutes: how many minutes of history to look back. Default 15.
        max_entries: cap on raw entries read from Cloud Logging before summarizing.

    Returns:
        A newline-joined list of "x<count>  <message>" lines, most frequent
        first, capped at 20 lines -- not the raw entries.
    """
    client = _logging_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    filter_str = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{service}" '
        f'severity>=WARNING '
        f'timestamp>="{cutoff}"'
    )

    seen: dict[str, int] = {}
    for entry in client.list_entries(filter_=filter_str, order_by=cloud_logging.DESCENDING, max_results=max_entries):
        payload = entry.payload if isinstance(entry.payload, dict) else {"message": str(entry.payload)}
        message = str(payload.get("message", payload)).strip()[:200]
        if message:
            seen[message] = seen.get(message, 0) + 1

    if not seen:
        return f"No WARNING+ log entries for {service} in the last {minutes} minutes."

    ranked = sorted(seen.items(), key=lambda kv: -kv[1])[:20]
    return "\n".join(f"x{count}  {message}" for message, count in ranked)
