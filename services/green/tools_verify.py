"""Tool for the Verifier agent: record an independent accuracy score for a
proposed fix.

Deliberately a separate agent from mitigation_agent in agent.py -- the
whole point is that the agent scoring a fix never sees mitigation_agent's
own reasoning, only the evidence left behind in Firestore (root cause,
diff, sandbox result), the same "no team grades its own work" principle
already used for the Red/Blue Judge.
"""
from __future__ import annotations

import state


def record_verification_score(incident_id: str, accuracy_score: int, rationale: str) -> str:
    """Record an independent accuracy/safety score for an incident's proposed fix.

    Args:
        incident_id: the incident being scored.
        accuracy_score: 0-4. 0 = the fix is clearly wrong or unsafe to
            apply. 2 = plausible but with real, specific doubts. 4 = high
            confidence the fix correctly addresses the stated root cause
            and is safe to apply.
        rationale: one or two sentences explaining the score, citing the
            specific evidence (sandbox result, log pattern, diff content)
            it's based on -- not a restatement of the fix itself.

    Returns:
        Confirmation text, or an "error: ..." string if the score is out
        of range.
    """
    if not 0 <= accuracy_score <= 4:
        return "error: accuracy_score must be an integer between 0 and 4"
    state.update_incident(incident_id, verification={"score": accuracy_score, "rationale": rationale})
    state.append_timeline(
        incident_id,
        event="verified",
        detail=f"score={accuracy_score}/4: {rationale}",
        actor="verifier_agent",
    )
    return f"Recorded verification score {accuracy_score}/4 for {incident_id}."
