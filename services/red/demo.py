#!/usr/bin/env python3
"""End-to-end demo of the closed self-improvement loop:

  1. Run the baseline target agent through the full attack campaign.
  2. Distill confirmed findings into guardrail patch proposals.
  3. Apply those patches to a fresh target instance (simulating the agent
     ingesting the red-team's feedback into its own policy).
  4. Re-run the SAME campaign against the patched target.
  5. Build one report comparing baseline vs. post-patch attack success rate.

This is the "self-improving platform + red team" story in one script:
the platform doesn't just get attacked, it gets measurably safer.
"""
from __future__ import annotations

import sys
from pathlib import Path

from redloop.attacks import load_seeds
from redloop.attacker import Attacker
from redloop.judge import Judge
from redloop.llm import get_backend
from redloop.orchestrator import Orchestrator
from redloop.report import build_report
from redloop.store import Store
from redloop.target import MockOpsAgent
from redloop.feedback import build_patch_proposals, render_proposals_markdown

ROOT = Path(__file__).parent


def main():
    backend = get_backend()  # auto: real Anthropic if ANTHROPIC_API_KEY is set, else mock
    print(f"Using backend: {backend.name}", file=sys.stderr)

    seeds = load_seeds(ROOT / "seeds" / "seeds.yaml")
    store = Store(ROOT / "runs" / "demo_findings.jsonl")

    # --- Baseline run ---
    baseline_target = MockOpsAgent()
    orch = Orchestrator(baseline_target, Attacker(backend), Judge(backend), store, max_turns=3)
    baseline_run_id = orch.run_campaign(seeds, run_id="baseline")

    all_findings = store.read_all()
    baseline_findings = [f for f in all_findings if f["run_id"] == baseline_run_id]
    n_violations = sum(1 for f in baseline_findings if f["violated"])
    print(f"Baseline: {n_violations}/{len(baseline_findings)} seeds got through.", file=sys.stderr)

    # --- Derive & apply patches ---
    proposals = build_patch_proposals(baseline_findings, min_severity=2)
    print("\n" + render_proposals_markdown(proposals) + "\n", file=sys.stderr)

    patched_target = MockOpsAgent()
    for p in proposals:
        patched_target.apply_patch(p.category, p.patch_text)

    # --- Re-run same campaign against patched target ---
    orch2 = Orchestrator(patched_target, Attacker(backend), Judge(backend), store, max_turns=3)
    patched_run_id = orch2.run_campaign(seeds, run_id="post-patch")

    all_findings = store.read_all()
    patched_findings = [f for f in all_findings if f["run_id"] == patched_run_id]
    n_violations_after = sum(1 for f in patched_findings if f["violated"])
    print(f"Post-patch: {n_violations_after}/{len(patched_findings)} seeds got through.", file=sys.stderr)

    # --- Report ---
    out = build_report(
        {"baseline": baseline_findings, "post-patch (self-improved)": patched_findings},
        ROOT / "runs" / "demo_report.html",
        title="RedLoop: Self-Improvement Loop Demo",
    )
    print(f"\nReport: {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
