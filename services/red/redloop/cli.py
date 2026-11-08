from __future__ import annotations

import argparse
import sys

from .attacks import load_seeds
from .attacker import Attacker
from .gcp_logging import get_logger, log
from .judge import Judge
from .llm import get_backend
from .orchestrator import Orchestrator
from .report import build_report
from .store import Store
from .target import MockOpsAgent
from .feedback import build_patch_proposals, render_proposals_markdown

_logger = get_logger(__name__)


def main(argv=None):
    p = argparse.ArgumentParser(prog="redloop")
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run a campaign against the target")
    run_p.add_argument("--seeds", default="seeds/seeds.yaml")
    run_p.add_argument("--store", default="runs/findings.jsonl")
    run_p.add_argument("--run-id", default=None)
    run_p.add_argument("--max-turns", type=int, default=3)
    run_p.add_argument("--backend", choices=["auto", "anthropic", "mock"], default="auto")
    run_p.add_argument("--no-attacker-fallback", action="store_true",
                       help="If the attacker LLM refuses, skip the turn instead of sending the seed/deterministic mutation")

    report_p = sub.add_parser("report", help="Build an HTML report from a store")
    report_p.add_argument("--store", default="runs/findings.jsonl")
    report_p.add_argument("--out", default="runs/report.html")
    report_p.add_argument("--run-id", action="append", help="Limit/label runs to include (repeatable)")

    feedback_p = sub.add_parser("feedback", help="Derive guardrail patch proposals from a store")
    feedback_p.add_argument("--store", default="runs/findings.jsonl")
    feedback_p.add_argument("--run-id", default=None)
    feedback_p.add_argument("--min-severity", type=int, default=2)

    args = p.parse_args(argv)

    if args.cmd == "run":
        backend_pref = None if args.backend == "auto" else args.backend
        backend = get_backend(backend_pref)
        log(_logger, "INFO", "job_started",
            event="job_started", cmd="run", backend=backend.name, seeds_file=args.seeds,
            requested_run_id=args.run_id, max_turns=args.max_turns)
        target = MockOpsAgent()
        attacker = Attacker(backend, fallback_on_refusal=not args.no_attacker_fallback)
        judge = Judge(backend)
        store = Store(args.store)
        orch = Orchestrator(target, attacker, judge, store, max_turns=args.max_turns)
        seeds = load_seeds(args.seeds)
        run_id = orch.run_campaign(seeds, run_id=args.run_id)
        log(_logger, "INFO", "job_finished", event="job_finished", cmd="run", run_id=run_id)
        print(f"[{backend.name}] campaign complete. run_id={run_id}  store={args.store}", file=sys.stderr)

    elif args.cmd == "report":
        store = Store(args.store)
        all_findings = store.read_all()
        if args.run_id:
            runs = {rid: [f for f in all_findings if f["run_id"] == rid] for rid in args.run_id}
        else:
            runs = {}
            for f in all_findings:
                runs.setdefault(f["run_id"], []).append(f)
        out = build_report(runs, args.out)
        print(f"report written to {out}", file=sys.stderr)

    elif args.cmd == "feedback":
        store = Store(args.store)
        all_findings = store.read_all()
        if args.run_id:
            all_findings = [f for f in all_findings if f["run_id"] == args.run_id]
        proposals = build_patch_proposals(all_findings, min_severity=args.min_severity)
        print(render_proposals_markdown(proposals))


if __name__ == "__main__":
    main()
