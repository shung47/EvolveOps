"""Structured logging for Cloud Logging.

Cloud Run (and GKE) automatically capture container stdout/stderr as log
entries. If a line is valid JSON with top-level "severity" and "message"
keys, Cloud Logging treats it as a *structured* entry: "severity" becomes
the entry's log level and every other key lands in `jsonPayload`, filterable
in Logs Explorer — e.g. `jsonPayload.run_id="abc123"` or
`jsonPayload.event="finding_confirmed" AND jsonPayload.attack_severity>=3`.

That's the entire mechanism used here: no `google-cloud-logging` client
library, no extra IAM role (`roles/logging.logWriter` is what Cloud Run's
own runtime service account already needs, not this code), no network call.
Every track the harness records — each attack turn, each judge verdict,
each patch proposal, each patch application — is a JSON line to stdout, and
Cloud Logging ingests it for free. Locally, the same lines just print as
JSON in the terminal.

This is deliberately separate from `store.py` (Firestore/JSONL): the store
is the queryable "current findings" table the report reads; Cloud Logging
is the immutable, timestamped audit trail across every run, and the
substrate for log-based alerting (e.g. a log-based metric counting
`attack_severity>=4` entries, alerting the moment one appears).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

# Attack-finding severity (0-4, from judge.py) -> Cloud Logging severity.
# Deliberately louder than default Python levels so a critical finding is
# visible in Logs Explorer without a custom filter.
ATTACK_SEVERITY_TO_LOG_LEVEL = {
    0: "INFO",
    1: "INFO",
    2: "WARNING",
    3: "ERROR",
    4: "CRITICAL",
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
        }
        # Correlate every line from one Cloud Run Job execution together.
        if os.environ.get("CLOUD_RUN_EXECUTION"):
            payload["cloud_run_execution"] = os.environ["CLOUD_RUN_EXECUTION"]
        if os.environ.get("CLOUD_RUN_TASK_INDEX"):
            payload["cloud_run_task_index"] = os.environ["CLOUD_RUN_TASK_INDEX"]
        fields = getattr(record, "json_fields", None)
        if fields:
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_configured = False


def get_logger(name: str = "redloop") -> logging.Logger:
    """Idempotent: safe to call from every module that wants to log."""
    global _configured
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        root = logging.getLogger()
        root.handlers = [handler]
        root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
        _configured = True
    return logging.getLogger(name)


def log(logger: logging.Logger, severity: str, message: str, **fields: Any) -> None:
    """log(logger, "WARNING", "attack succeeded", run_id=..., seed_id=..., attack_severity=3)"""
    logger.log(logging.getLevelName(severity), message, extra={"json_fields": fields})
