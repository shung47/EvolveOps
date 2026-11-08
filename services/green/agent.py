"""ADK coordinator + specialist agents for the Green Team.

One Coordinator agent is the single entry point, reached either by the
automatic Cloud Logging trigger or by a human typing into the dashboard's
"AI assistant" chat (see app.py's `/incidents` and `/chat` routes). It calls out
to four specialist agents as tools -- `AgentTool`, not `sub_agents` -- so
it stays in control and decides which one(s) to call, in what order, for
both the automatic pipeline and ad-hoc human requests:

  - diagnostic_agent  -- reads Cloud Logging and past similar incidents
                          (see tools_rag.py), finds the root cause
  - mitigation_agent  -- reads the repo, drafts a fix, tests it in an
                          isolated sandbox (see tools_sandbox.py), then
                          asks for approval
  - verifier_agent    -- independently scores the proposed fix's
                          accuracy and safety, reading only the Firestore
                          record left behind -- never mitigation_agent's
                          own reasoning. Same "no team grades its own
                          work" principle as the Red/Blue Judge.
  - reporter_agent    -- summarizes the incident timeline into a report,
                          then records a memory of it for future RAG
                          retrieval (see tools_rag.py)

RAG: `tools_rag.py` backs incident memory with Firestore's native vector
search (`find_nearest`), not a separate vector DB -- consistent with
Firestore already being this service's one datastore. It needs a vector
index created before it works; see README.md's "RAG" section.

Deliberate deviation from the "textbook" ADK long-running-tool pattern
------------------------------------------------------------------------
`request_human_approval` (in tools_code.py) is a plain tool, not a
`LongRunningFunctionTool` that keeps the agent run suspended until a
matching FunctionResponse comes back. ADK's long-running-tool resume needs
the session to still exist in the SessionService when the response
arrives: `DatabaseSessionService` needs a real SQL database we don't have
provisioned for this project, and `InMemorySessionService` (what
app.py actually uses) doesn't survive a Cloud Run instance recycling. A
human approval can take anywhere from two minutes to two days -- longer
than it's safe to bet a Cloud Run instance stays alive and reachable.

So instead: `request_human_approval` writes the proposed fix to Firestore,
marks the incident `awaiting_approval`, and returns immediately -- ending
that agent turn cleanly, no suspended session. When the dashboard's
"Approve & execute" button fires later, on any instance, `app.py`'s
`POST /approvals/<id>` route runs `open_pull_request` *directly*, without
going back through an LLM call: by the time a human has approved an
already-drafted diff, there's no more reasoning left to do.
"""
from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool

import state
from tools_code import propose_patch, read_repo_file, request_human_approval
from tools_logging import fetch_incident_logs
from tools_rag import record_incident_memory, search_similar_incidents
from tools_sandbox import run_sandbox_test
from tools_verify import record_verification_score

MODEL = os.environ.get("GREEN_TEAM_MODEL", "gemini-3.6-flash")


diagnostic_agent = LlmAgent(
    name="diagnostic_agent",
    model=MODEL,
    description="Reads Cloud Logging and past incidents to find one incident's root cause.",
    instruction=(
        "You investigate one incident at a time. You will be told "
        "an incident_id and a service name. First call "
        "`search_similar_incidents` with a short description of the "
        "symptom (if you don't have one yet, use the service name and "
        "whatever's in the alert) -- a past incident with the same root "
        "cause can save you from re-deriving it from logs alone. Then "
        "call `fetch_incident_logs` for the service and look for the "
        "dominant error pattern. Reply with a short root-cause finding in "
        "plain text (2-3 sentences); mention if it matches a past "
        "incident. You do not have a tool to record the finding -- just "
        "say it; the coordinator that called you is responsible for what "
        "happens next. Treat `search_similar_incidents` returning nothing "
        "or an error as normal -- proceed with logs alone."
    ),
    tools=[search_similar_incidents, fetch_incident_logs],
)

mitigation_agent = LlmAgent(
    name="mitigation_agent",
    model=MODEL,
    description=(
        "Reads the relevant source file, drafts a minimal fix as a unified "
        "diff, tests it in an isolated sandbox, and requests human "
        "approval before anything ships."
    ),
    instruction=(
        "You will be given an incident_id and a root-cause finding. "
        "Optionally call `search_similar_incidents` with the root cause "
        "first -- if a past incident describes a fix for the same thing, "
        "it can point you straight at the right change instead of "
        "guessing. Then call `read_repo_file` to see the current source "
        "for the file the root cause points to, then call `propose_patch` "
        "with a small unified diff plus one-line risk and rollback "
        "summaries.\n\n"
        "Then call `run_sandbox_test` with the incident_id. This applies "
        "your diff in an isolated environment and, if a test command is "
        "configured, runs it -- you do not run or guess at test results "
        "yourself.\n"
        "- If it PASSES: call `request_human_approval` once and stop.\n"
        "- If it FAILS: read the failure output, call `propose_patch` "
        "again with a revised diff addressing it, then `run_sandbox_test` "
        "once more. You get one retry. If it still fails (or the result "
        "comes back inconclusive) after that, call "
        "`request_human_approval` anyway, but say plainly in your final "
        "reply that the sandbox test did not pass -- a human should see "
        "that before approving, not be told it's safe when it isn't. "
        "Never call `request_human_approval` more than once per incident, "
        "and never skip `run_sandbox_test` before the first call to it."
    ),
    tools=[search_similar_incidents, read_repo_file, propose_patch, run_sandbox_test, request_human_approval],
)

verifier_agent = LlmAgent(
    name="verifier_agent",
    model=MODEL,
    description=(
        "Independently scores how accurate and safe a proposed fix is, "
        "reading only the Firestore record -- never mitigation_agent's "
        "own reasoning."
    ),
    instruction=(
        "You will be given an incident_id. Call `get_incident` to read "
        "its root_cause, proposed_fix (file_path, diff, risk, rollback), "
        "and sandbox_test result directly from Firestore -- do not rely "
        "on anything the caller told you about the incident, look at the "
        "stored record yourself. Call `get_timeline` too for more context "
        "on how the investigation went if you want it.\n\n"
        "Judge, skeptically: does the diff plausibly fix the stated root "
        "cause? Does the sandbox_test result (if any) support that? Is "
        "there an obvious safety or security concern the fix missed? Your "
        "only job is to catch what the agent that wrote the fix might "
        "have missed or rationalized away -- do not simply agree with it.\n\n"
        "Then call `record_verification_score` exactly once with a 0-4 "
        "score (0 = clearly wrong or unsafe, 4 = high confidence correct "
        "and safe) and a short rationale citing specific evidence from "
        "what you read, not a restatement of the fix."
    ),
    tools=[state.get_incident, state.get_timeline, record_verification_score],
)

reporter_agent = LlmAgent(
    name="reporter_agent",
    model=MODEL,
    description="Summarizes a resolved incident into a report and records it for future RAG retrieval.",
    instruction=(
        "You will be given an incident_id. Call `get_timeline` for it and "
        "write a short post-mortem in plain text: what happened, the root "
        "cause, the fix that was applied, who approved it and when, and "
        "the outcome. Keep it under 300 words. If the timeline shows the "
        "incident isn't resolved yet (no `pr_opened` or later event), say "
        "so instead of inventing an ending -- and do not call "
        "`record_incident_memory` for an unresolved incident.\n\n"
        "If it is resolved, also call `record_incident_memory` once with "
        "a 2-4 sentence summary (symptom, root cause, fix, outcome) -- "
        "shorter and more focused than the full report, written so it's "
        "useful standalone to a future diagnostic_agent searching for "
        "incidents like this one."
    ),
    tools=[state.get_timeline, record_incident_memory],
)

coordinator_agent = LlmAgent(
    name="coordinator_agent",
    model=MODEL,
    description=(
        "Single entry point for the Green Team. Routes incidents "
        "to the right specialist agent, whether triggered automatically or "
        "by a human operator's ad-hoc request."
    ),
    instruction=(
        "You receive either (a) an automatic incident alert naming an "
        "incident_id and service, or (b) a free-form request from a human "
        "operator typing in the dashboard chat.\n\n"
        "For (a): call diagnostic_agent with the incident_id and service, "
        "then pass its finding to mitigation_agent along with the "
        "incident_id. Once mitigation_agent requests approval, call "
        "verifier_agent with the same incident_id -- it reads the "
        "Firestore record itself and scores the fix independently, you "
        "don't need to pass it anything but the incident_id. Stop after "
        "that -- do not call reporter_agent yet, the incident isn't "
        "resolved (it's awaiting approval, not finished).\n\n"
        "For (b): work out which specialist(s) the request actually needs "
        "and call only those -- you do not have to run the full pipeline. "
        "Re-run diagnosis if asked to. Call reporter_agent if asked for a "
        "report or summary. Call verifier_agent if asked to double-check "
        "or re-score a fix. If the question doesn't need a specialist "
        "(e.g. \"what does the Mitigation agent do\"), answer directly."
    ),
    tools=[
        AgentTool(agent=diagnostic_agent),
        AgentTool(agent=mitigation_agent),
        AgentTool(agent=verifier_agent),
        AgentTool(agent=reporter_agent),
    ],
)
