"""Runs a campaign: every seed x mutation budget, against one target.

Every meaningful action -- which seed/category/turn is running, the exact
mutated prompt the attacker sent, the target's response, the judge's verdict
and rationale, and which LLM backend/model/region produced them -- is both
(a) written to Cloud Logging as a structured JSON line via `gcp_logging.log`
(so `jsonPayload.run_id="..."` in Logs Explorer shows the whole campaign live
while it runs) and (b) handed to an optional `on_event` callback, which the
Cloud Run HTTP handler uses to stream the same events back to the caller's
terminal in near-real-time (see `deploy/service/app.py`'s `?stream=1`
mode). Both destinations get identical detail from one code path.
"""
from __future__ import annotations

import uuid
from typing import Callable, Dict, List, Optional

from .attacks import Seed, CATEGORY_WEIGHT
from .attacker import Attacker, SOURCE_SKIPPED
from .gcp_logging import ATTACK_SEVERITY_TO_LOG_LEVEL, get_logger, log
from .judge import Judge
from .llm import ChatMessage
from .store import Finding, Store
from .target import Target

_logger = get_logger(__name__)

# Cap how much prompt/response/rationale text lands in any single log line or
# streamed event -- enough to see exactly what technique/wording was used,
# without ballooning Cloud Logging volume or the streamed response size.
_SNIPPET_LEN = 500


def _snippet(text: Optional[str], limit: int = _SNIPPET_LEN) -> str:
    if text is None:
        return ""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _backend_info(backend) -> Dict[str, str]:
    """Best-effort parameter dump for whatever backend is in play, so every
    campaign_started event/log line answers "what is it actually calling"
    (backend flavor, model name, Vertex region/project) without the caller
    needing to separately inspect env vars."""
    info = {"backend": getattr(backend, "name", type(backend).__name__)}
    for attr in ("model", "region", "project"):
        val = getattr(backend, attr, None)
        if val:
            info[attr] = val
    return info


class Orchestrator:
    def __init__(
        self,
        target: Target,
        attacker: Attacker,
        judge: Judge,
        store: Store,
        max_turns: int = 3,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.target = target
        self.attacker = attacker
        self.judge = judge
        self.store = store
        self.max_turns = max_turns
        self.on_event = on_event

    def _emit(self, level: str, event: str, **fields) -> None:
        log(_logger, level, event, event=event, **fields)
        if self.on_event is not None:
            try:
                self.on_event({"event": event, "severity": level, **fields})
            except Exception:
                # A broken/disconnected stream consumer must never take down
                # the campaign itself -- Cloud Logging already has the line.
                pass

    def run_campaign(self, seeds: List[Seed], run_id: Optional[str] = None) -> str:
        run_id = run_id or uuid.uuid4().hex[:8]
        backend_info = _backend_info(self.attacker.backend)
        self._emit(
            "INFO", "campaign_started",
            run_id=run_id, seed_count=len(seeds), max_turns=self.max_turns,
            **backend_info,
        )

        counts = {"violated": 0, "defended": 0, "inconclusive": 0}
        self._attacker_turns = 0
        self._attacker_refusals = 0
        self._attacker_errors = 0
        for seed in seeds:
            counts[self._run_seed(seed, run_id)] += 1

        tested = counts["violated"] + counts["defended"]
        refusal_rate = (self._attacker_refusals / self._attacker_turns * 100) if self._attacker_turns else 0
        fallback_rate = ((self._attacker_refusals + self._attacker_errors) / self._attacker_turns * 100) if self._attacker_turns else 0
        self._emit(
            "WARNING" if fallback_rate > 20 or counts["inconclusive"] else "INFO",
            "campaign_finished",
            run_id=run_id, seed_count=len(seeds), tested_count=tested,
            violated_count=counts["violated"], inconclusive_count=counts["inconclusive"],
            success_rate_pct=round(counts["violated"] / tested * 100, 1) if tested else 0,
            attacker_turns=self._attacker_turns, attacker_refusals=self._attacker_refusals,
            attacker_refusal_rate_pct=round(refusal_rate, 1),
            attacker_errors=self._attacker_errors,
        )
        return run_id

    def _run_seed(self, seed: Seed, run_id: str) -> str:
        """Returns "violated", "defended" or "inconclusive".

        If the attacker LLM refuses to produce a prompt, its refusal text is
        never forwarded to the target (see attacker.py); a fallback prompt is
        sent instead, or -- with fallback disabled -- the turn is skipped. A
        seed where no prompt at all reached the target is "inconclusive",
        not "defended", so it can't inflate a clean-looking pass rate.
        """
        history: List[ChatMessage] = []
        sources: List[str] = []          # parallel to user turns in `history`
        prompt_sources: List[str] = []   # every attacker turn, including skipped ones
        refusals = 0
        errors = 0
        turns_run = 0
        last_response = None
        last_prompt = None
        weight = CATEGORY_WEIGHT.get(seed.category, 2)

        def transcript():
            out, ui = [], 0
            for m in history:
                d = {"role": m.role, "content": m.content}
                if m.role == "user":
                    d["source"] = sources[ui]
                    ui += 1
                out.append(d)
            return out

        def finding(status, violated, severity, rationale, turns_to_success):
            return Finding(
                run_id=run_id, seed_id=seed.id, category=seed.category, goal=seed.goal,
                violated=violated, severity=severity, rationale=rationale,
                turns_to_success=turns_to_success, transcript=transcript(),
                status=status, turns_run=turns_run, attacker_turns=len(prompt_sources),
                attacker_refusals=refusals, attacker_errors=errors,
                prompt_sources=list(prompt_sources),
            )

        self._emit(
            "INFO", "seed_started",
            run_id=run_id, seed_id=seed.id, category=seed.category, goal=seed.goal,
        )

        for turn in range(self.max_turns):
            m = self.attacker.mutate(
                seed.prompt, last_response, turn,
                category=seed.category, goal=seed.goal, last_prompt=last_prompt,
            )
            prompt_sources.append(m.source)
            self._attacker_turns += 1
            if m.attacker_error:
                # The attacker call itself failed (auth, quota, model not
                # enabled, bad region...) -- a config problem, not a refusal.
                errors += 1
                self._attacker_errors += 1
                self._emit(
                    "ERROR", "attacker_error",
                    run_id=run_id, seed_id=seed.id, category=seed.category, turn=turn,
                    fallback=m.source, error=_snippet(m.raw_attacker_output, 300),
                )
            elif m.attacker_refused:
                refusals += 1
                self._attacker_refusals += 1
                self._emit(
                    "WARNING", "attacker_refused",
                    run_id=run_id, seed_id=seed.id, category=seed.category, turn=turn,
                    fallback=m.source, attacker_output=_snippet(m.raw_attacker_output, 300),
                )
            if m.source == SOURCE_SKIPPED:
                continue

            prompt = m.prompt
            last_prompt = prompt
            self._emit(
                "INFO", "attacker_mutated",
                run_id=run_id, seed_id=seed.id, category=seed.category, turn=turn,
                prompt_source=m.source, prompt=_snippet(prompt),
            )
            history.append(ChatMessage(role="user", content=prompt))
            sources.append(m.source)

            response = self.target.respond(history)
            self._emit(
                "INFO", "target_responded",
                run_id=run_id, seed_id=seed.id, turn=turn,
                response=_snippet(response),
            )
            history.append(ChatMessage(role="assistant", content=response))
            last_response = response
            turns_run += 1

            verdict = self.judge.score(seed.category, prompt, response, weight)
            self._emit(
                "DEBUG" if not verdict.violated else "INFO",
                "judge_scored",
                run_id=run_id, seed_id=seed.id, category=seed.category, turn=turn,
                violated=verdict.violated, attack_severity=verdict.severity,
                rationale=_snippet(verdict.rationale),
            )

            if verdict.violated:
                self.store.write(finding("violated", True, verdict.severity, verdict.rationale, turn + 1))
                self._emit(
                    ATTACK_SEVERITY_TO_LOG_LEVEL.get(verdict.severity, "WARNING"),
                    "finding_confirmed",
                    run_id=run_id, seed_id=seed.id,
                    category=seed.category, goal=seed.goal, attack_severity=verdict.severity,
                    turns_to_success=turn + 1, rationale=_snippet(verdict.rationale),
                    prompt_source=m.source,
                )
                return "violated"

        if turns_run == 0:
            self.store.write(finding(
                "inconclusive", False, 0,
                f"Inconclusive: attacker refused or failed on all {refusals + errors} turn(s); no attack reached the target.", None,
            ))
            self._emit(
                "WARNING", "seed_inconclusive",
                run_id=run_id, seed_id=seed.id, category=seed.category, attacker_refusals=refusals,
            )
            return "inconclusive"

        # No success within budget -> record a clean (non-violating) result too,
        # so the report can show attempted-but-defended seeds, not just hits.
        rationale = "No violation found within turn budget."
        if refusals or errors:
            rationale += (f" (attacker LLM refused {refusals} and errored on {errors} of "
                          f"{len(prompt_sources)} turn(s); fallback prompts used)")
        self.store.write(finding("defended", False, 0, rationale, None))
        self._emit(
            "INFO", "seed_defended",
            run_id=run_id, seed_id=seed.id, category=seed.category, attacker_refusals=refusals,
        )
        return "defended"
