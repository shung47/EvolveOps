"""Red-team attacker + HTTP trigger, combined into one Cloud Run service.

A simpler alternative to the Job + trigger design in deploy/job/ and
deploy/trigger_service/, for environments where only Cloud Run *Services*
are available. Folding the trigger and the campaign runner into one
service keeps the deploy to a single command: POST /run
*is* the HTTPS trigger, and it runs the campaign synchronously (a 12-seed
campaign finishes in well under Cloud Run's request timeout) and returns
the full result -- findings, HTML report, and patch proposals -- in one
response, so there's no separate execution-polling step needed for this
quick-test path.

POST /run?stream=1 opts into a second response mode: instead of one
buffered JSON body at the end, the response streams a newline-delimited
JSON ("NDJSON") line the moment each action happens -- which seed/category/
turn is running, the attacker's exact mutated prompt, the target's
response, the judge's verdict -- so `curl -N ... "/run?stream=1"` shows the
campaign unfold live in the terminal instead of going quiet for the whole
duration. The very last line is the same full result object the
non-streaming mode returns as its only body (tagged "event": "result"), so
existing tooling that just wants the final JSON can always take the last
line. Every one of these events is also written to Cloud Logging as a
structured line regardless of whether streaming was requested (see
`redloop/orchestrator.py` / `gcp_logging.py`), so Logs Explorer shows
the same live detail for triggers that didn't ask for a streamed response
(e.g. a scheduled/cron trigger with no one watching a terminal).
"""
import http.server
import json
import os
import socketserver
import sys
import tempfile
import urllib.parse
from pathlib import Path

from dataclasses import asdict

# Deployed: deploy.sh copies the `redloop` package and seeds.json next to this
# file. Running locally from a checkout: fall back to the repo root.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(1, str(_REPO_ROOT))

from redloop.attacks import load_seeds
from redloop.attacker import Attacker
from redloop.feedback import build_patch_proposals, render_proposals_markdown
from redloop.gcp_logging import ATTACK_SEVERITY_TO_LOG_LEVEL, get_logger, log
from redloop.judge import Judge
from redloop.llm import get_attacker_backend, get_backend
from redloop.orchestrator import Orchestrator
from redloop.report import build_report
from redloop.store import Store
from redloop.target import HTTPTarget

TRIGGER_KEY = os.environ.get("TRIGGER_KEY", "")
TARGET_URL = os.environ.get("TARGET_URL", "")
TARGET_TOKEN = os.environ.get("TARGET_TOKEN", "")  # legacy static-token mode (v1 mock only)
TARGET_USER_ID = os.environ.get("TARGET_USER_ID", "")  # session-login mode (evolveops-blue)
TARGET_PASSWORD = os.environ.get("TARGET_PASSWORD", "")
PORT = int(os.environ.get("PORT", 8080))
SEEDS_PATH = _HERE / "seeds.json"
if not SEEDS_PATH.exists():
    SEEDS_PATH = _REPO_ROOT / "seeds" / "seeds.json"

_logger = get_logger(__name__)


def run_campaign(run_id: str, mode: str, max_turns: int, on_event=None) -> dict:
    backend = get_backend()  # judge: Vertex Gemini/Claude if VERTEX_PROJECT is set, else Anthropic API if ANTHROPIC_API_KEY is set, else mock
    attacker_backend = get_attacker_backend()  # ATTACKER_MODEL if set, else the same as the judge
    # TARGET_URL is the target's *base* URL; HTTPTarget appends /login and /chat.
    target = HTTPTarget(base_url=TARGET_URL, user_id=TARGET_USER_ID,
                        password=TARGET_PASSWORD, token=TARGET_TOKEN)
    log(_logger, "INFO", "job_started",
        event="job_started", run_id=run_id, mode=mode, max_turns=max_turns,
        backend=backend.name, judge_model=getattr(backend, "model", None),
        attacker_backend=attacker_backend.name, attacker_model=getattr(attacker_backend, "model", None),
        target_url=TARGET_URL,
        target_auth_mode=target.auth_mode, target_user_id=TARGET_USER_ID or None)
    attacker = Attacker(attacker_backend)
    judge = Judge(backend)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        store = Store(tmp / "findings.jsonl")
        orch = Orchestrator(target, attacker, judge, store, max_turns=max_turns, on_event=on_event)
        seeds = load_seeds(SEEDS_PATH)
        rid = orch.run_campaign(seeds, run_id=run_id)
        findings = [f for f in store.read_all() if f["run_id"] == rid]
        report_path = build_report({rid: findings}, tmp / "report.html", title=f"RedLoop Report — {rid}")
        report_html = report_path.read_text()
    proposals = build_patch_proposals(findings, min_severity=2)

    # One consolidated, structured Cloud Logging entry per completed campaign:
    # every finding's category/severity/rationale and every proposed patch,
    # plus the summary counts, in a single jsonPayload keyed by run_id. The
    # per-turn events emitted during the run (attacker_mutated, judge_scored,
    # finding_confirmed, patch_proposed, ...) are still logged individually
    # for a live/streaming audit trail, but they're scattered across many
    # lines -- this entry exists so a log consumer (a Logs Explorer query for
    # `jsonPayload.event="campaign_result"`, a log sink into BigQuery, or an
    # LLM/agent polling the Cloud Logging API to review results) can fetch
    # ONE entry and get the whole picture, without reassembling it from the
    # rest of the run's log lines. `report_html` is deliberately left out --
    # Cloud Logging entries cap at 256 KiB, rendered HTML isn't something a
    # log consumer can analyze, and the same information is already present
    # here in structured form.
    max_severity = max((f["severity"] for f in findings), default=0)
    violated_count = sum(1 for f in findings if f["violated"])
    inconclusive_count = sum(1 for f in findings if f.get("status") == "inconclusive")
    tested_count = len(findings) - inconclusive_count
    attacker_turns = sum(f.get("attacker_turns", 0) for f in findings)
    attacker_refusals = sum(f.get("attacker_refusals", 0) for f in findings)
    attacker_errors = sum(f.get("attacker_errors", 0) for f in findings)
    refusal_rate_pct = round(attacker_refusals / attacker_turns * 100, 1) if attacker_turns else 0
    fallback_rate_pct = round((attacker_refusals + attacker_errors) / attacker_turns * 100, 1) if attacker_turns else 0
    stats = {
        "total": tested_count,              # seeds that actually reached the target
        "seed_count": len(findings),
        "inconclusive": inconclusive_count,
        "violated": violated_count,
        "success_rate_pct": round(violated_count / tested_count * 100, 1) if tested_count else 0,
        "attacker_turns": attacker_turns,
        "attacker_refusals": attacker_refusals,
        "attacker_refusal_rate_pct": refusal_rate_pct,
        "attacker_errors": attacker_errors,
        "run_valid": inconclusive_count == 0 and fallback_rate_pct <= 20,
        "judge_model": getattr(backend, "model", None),
        "attacker_backend": attacker_backend.name,
        "attacker_model": getattr(attacker_backend, "model", None),
    }
    log(
        _logger,
        ATTACK_SEVERITY_TO_LOG_LEVEL.get(max_severity, "INFO") if violated_count else "INFO",
        "campaign_result",
        event="campaign_result",
        run_id=rid,
        mode=mode,
        backend=backend.name,
        **stats,
        max_severity=max_severity,
        findings=[
            {
                "seed_id": f["seed_id"],
                "category": f["category"],
                "goal": f["goal"],
                "violated": f["violated"],
                "status": f.get("status"),
                "attacker_refusals": f.get("attacker_refusals", 0),
                "attacker_errors": f.get("attacker_errors", 0),
                "severity": f["severity"],
                "rationale": f["rationale"],
                "turns_to_success": f["turns_to_success"],
            }
            for f in findings
        ],
        proposals=[asdict(p) for p in proposals],
    )

    log(_logger, "INFO", "job_finished", event="job_finished", run_id=rid, backend=backend.name)
    return {
        "run_id": rid,
        "mode": mode,
        "backend": backend.name,
        **stats,
        "findings": findings,
        "proposals_markdown": render_proposals_markdown(proposals),
        "report_html": report_html,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    # Cloud Run's front end (GFE) speaks HTTP/1.1 to the container and appears
    # to reject/mangle a plain HTTP/1.0 response on the streaming path -- that
    # was the real cause of the second 403 (the first 403 was the earlier
    # hand-rolled Transfer-Encoding: chunked framing; removing that alone
    # left us on BaseHTTPRequestHandler's HTTP/1.0 default, which GFE also
    # rejected). Declaring HTTP/1.1 plus an explicit "Connection: close" is a
    # standards-compliant way to send a response with no Content-Length and no
    # Transfer-Encoding: framing is "read until the connection closes"
    # (RFC 7230 3.3.3 #7), which is exactly what a streaming NDJSON body needs.
    protocol_version = "HTTP/1.1"

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return bool(TRIGGER_KEY) and self.headers.get("X-Trigger-Key", "") == TRIGGER_KEY

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/run":
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except Exception:
            body = {}
        run_id = body.get("run_id") or f"triggered-{os.urandom(4).hex()}"
        mode = body.get("mode", "baseline")
        max_turns = int(body.get("max_turns", 3))

        # Every framing fix tried so far (HTTP/1.0 close-delimited,
        # HTTP/1.1 + Connection: close, HTTP/1.1 real chunked
        # Transfer-Encoding) hit the identical GFE 403 -- meaning the
        # response framing was never the actual variable. Two things stayed
        # constant across every attempt: the "?stream=1" query string on the
        # URL, and the "application/x-ndjson" response Content-Type. This
        # accepts the streaming flag from the JSON body too (`"stream": true`)
        # so a caller can avoid the query string entirely while we isolate
        # which of the two was actually the trouble.
        streaming = urllib.parse.parse_qs(parsed.query).get("stream", ["0"])[0] in ("1", "true", "yes") or bool(body.get("stream"))
        if not streaming:
            try:
                result = run_campaign(run_id, mode, max_turns)
            except Exception as exc:  # noqa: BLE001 -- surface the real error to the caller
                # Log first, so the real cause survives even if the caller is gone.
                log(_logger, "ERROR", "job_failed", event="job_failed", run_id=run_id,
                    error=str(exc), error_type=type(exc).__name__)
                try:
                    self._send_json(500, {"error": str(exc)})
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
                return
            try:
                self._send_json(200, result)
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                log(_logger, "WARNING", "client_disconnected", event="client_disconnected",
                    run_id=run_id, error=str(exc))
            return

        # Streaming mode: plain NDJSON, one line per action the moment it
        # happens, terminated by the same full result object the
        # non-streaming mode returns as its whole body.
        #
        # This framing took three tries to get right against Cloud Run's
        # front end (GFE), and all three symptoms looked identical (a 403
        # Forbidden HTML page from the proxy, not from this app):
        #   1. "Transfer-Encoding: chunked" + hand-rolled chunk framing,
        #      but still on BaseHTTPRequestHandler's HTTP/1.0 default --
        #      chunked transfer-encoding isn't a legal thing to declare on
        #      an HTTP/1.0 response at all, so GFE rejected it outright.
        #   2. Dropped Transfer-Encoding entirely and relied on "body ends
        #      when the connection closes" (with protocol_version bumped to
        #      "HTTP/1.1" and "Connection: close" sent) -- legal per RFC
        #      7230 3.3.3 #7, but GFE still rejected it: its pass-through
        #      pipeline apparently needs an explicit length or explicit
        #      chunked framing, not a close-delimited body.
        #   3. Real HTTP/1.1 chunked Transfer-Encoding *and*
        #      protocol_version = "HTTP/1.1" together -- the combination
        #      attempt 1 never actually tried, since it was still declaring
        #      chunked framing while defaulting to HTTP/1.0. Each chunk is
        #      "<hex length>\r\n<chunk bytes>\r\n", terminated by the
        #      standard zero-length final chunk "0\r\n\r\n". Verified byte-
        #      correct locally (curl decodes it with zero artifacts) --
        #      STILL a 403 from GFE against the real Cloud Run deployment.
        #
        # That means response framing was never the actual variable -- three
        # very different, individually-correct framings can't all coincidentally
        # trigger the identical error. Two things stayed constant across every
        # attempt instead: the "?stream=1" query string on the URL, and the
        # "application/x-ndjson" Content-Type (not a common/allowlisted MIME
        # type, unlike plain /run's "application/json"). This attempt changes
        # Content-Type to the boring, universally-recognized "application/json"
        # (do_POST above also now accepts the streaming flag from the request
        # body as an alternative to the query string) to isolate whether either
        # of those -- rather than anything about the byte-level framing -- is
        # what a WAF/edge policy in front of Cloud Run is actually objecting to.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        # If the client connection dies mid-campaign (e.g. Cloud Run's request
        # timeout), stop writing to it but let the campaign finish, so every
        # seed still runs and campaign_result is still logged.
        stream_state = {"dead": False}

        def _write_chunk(obj):
            if stream_state["dead"]:
                return
            data = (json.dumps(obj, default=str) + "\n").encode()
            try:
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                stream_state["dead"] = True
                log(_logger, "WARNING", "stream_client_disconnected",
                    event="stream_client_disconnected", run_id=run_id, error=str(exc))

        try:
            result = run_campaign(run_id, mode, max_turns, on_event=_write_chunk)
            _write_chunk({"event": "result", **result})
        except Exception as exc:  # noqa: BLE001
            log(_logger, "ERROR", "job_failed", event="job_failed", run_id=run_id,
                error=str(exc), error_type=type(exc).__name__)
            _write_chunk({"event": "error", "error": str(exc)})
        finally:
            if not stream_state["dead"]:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionError, OSError):
                    pass

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
