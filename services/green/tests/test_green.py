"""Offline tests for the green team's multi-agent SRE service.

No Gemini, GitHub or GCP calls: Firestore is replaced by an in-memory fake,
and the LLM agents are only inspected structurally (who can call what).
"""
from __future__ import annotations

import os

os.environ.setdefault("GREEN_TEAM_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "local")

import pytest

import state


# ------------------------------------------------------- fake Firestore ----

class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def set(self, data, merge=False):
        cur = self.store.docs.get(self.path, {}) if merge else {}
        cur.update(data)
        self.store.docs[self.path] = cur

    def get(self):
        return _Snap(self.store.docs.get(self.path))

    def collection(self, name):
        return _Col(self.store, self.path + (name,))


class _Col:
    def __init__(self, store, path):
        self.store, self.path = store, path
        self._order = None

    def document(self, doc_id):
        return _Doc(self.store, self.path + (doc_id,))

    def add(self, data):
        self.store.subdocs.setdefault(self.path, []).append(dict(data))

    def order_by(self, field):
        self._order = field
        return self

    def stream(self):
        rows = list(self.store.subdocs.get(self.path, []))
        if self._order:
            rows.sort(key=lambda r: r[self._order])
        return [_Snap(r) for r in rows]


class FakeFirestore:
    def __init__(self):
        self.docs, self.subdocs = {}, {}

    def collection(self, name):
        return _Col(self, (name,))


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    db = FakeFirestore()
    monkeypatch.setattr(state, "_db", db)
    return db


def events(incident_id):
    return [(e["event"], e["actor"]) for e in state.get_timeline(incident_id)]


# ----------------------------------------------------------- tool layer ----

def test_approval_requires_a_drafted_fix_first():
    from tools_code import request_human_approval

    state.create_incident("inc-1", title="5xx spike", service="warden")
    assert request_human_approval("inc-1").startswith("error:")
    assert state.get_incident("inc-1")["status"] == "investigating"


def test_fix_then_approval_moves_incident_to_awaiting_approval():
    from tools_code import propose_patch, request_human_approval

    state.create_incident("inc-2", title="5xx spike", service="warden")
    propose_patch("inc-2", "app.py", "--- a\n+++ b\n", "low: one-line guard", "revert the commit")
    assert "awaiting_approval" in request_human_approval("inc-2")

    inc = state.get_incident("inc-2")
    assert inc["status"] == "awaiting_approval"
    assert inc["proposed_fix"]["file_path"] == "app.py"
    assert [e for e, _ in events("inc-2")] == ["incident_created", "fix_drafted", "approval_requested"]


@pytest.mark.parametrize("score", [-1, 5])
def test_verifier_score_is_range_checked(score):
    from tools_verify import record_verification_score

    state.create_incident("inc-3", title="t", service="warden")
    assert record_verification_score("inc-3", score, "x").startswith("error:")
    assert "verification" not in state.get_incident("inc-3")


def test_verifier_score_is_recorded_under_its_own_identity():
    from tools_verify import record_verification_score

    state.create_incident("inc-4", title="t", service="warden")
    record_verification_score("inc-4", 3, "sandbox passed; diff matches the logged NPE")
    assert state.get_incident("inc-4")["verification"] == {
        "score": 3, "rationale": "sandbox passed; diff matches the logged NPE",
    }
    assert ("verified", "verifier_agent") in events("inc-4")


def test_log_tool_dedupes_and_ranks_before_anything_reaches_the_llm(monkeypatch):
    import tools_logging

    class Entry:
        def __init__(self, msg):
            self.payload = {"message": msg}

    class Client:
        def list_entries(self, **_):
            return [Entry("NPE in /chat")] * 5 + [Entry("timeout calling Firestore")] * 2 + [Entry("")]

    monkeypatch.setattr(tools_logging, "_client", Client())
    out = tools_logging.fetch_incident_logs("warden").splitlines()
    assert out == ["x5  NPE in /chat", "x2  timeout calling Firestore"]


# ---------------------------------------------------- multi-agent wiring ----

def _tool_names(agent):
    names = []
    for t in agent.tools:
        names.append(getattr(t, "name", None) or getattr(t, "__name__", None) or getattr(getattr(t, "agent", None), "name", None))
    return set(names)


def test_coordinator_orchestrates_four_specialists_as_tools():
    from google.adk.tools import AgentTool
    import agent

    assert all(isinstance(t, AgentTool) for t in agent.coordinator_agent.tools)
    assert {t.agent.name for t in agent.coordinator_agent.tools} == {
        "diagnostic_agent", "mitigation_agent", "verifier_agent", "reporter_agent",
    }


def test_verifier_is_independent_of_the_agent_that_wrote_the_fix():
    import agent

    verifier = _tool_names(agent.verifier_agent)
    assert verifier == {"get_incident", "get_timeline", "record_verification_score"}
    # it can read the stored evidence, but can't draft, test or ship a fix
    assert not verifier & {"propose_patch", "run_sandbox_test", "request_human_approval", "read_repo_file"}


def test_only_mitigation_can_draft_and_only_after_a_sandbox_tool_exists():
    import agent

    for a in (agent.diagnostic_agent, agent.verifier_agent, agent.reporter_agent):
        assert "propose_patch" not in _tool_names(a)
    assert {"propose_patch", "run_sandbox_test", "request_human_approval"} <= _tool_names(agent.mitigation_agent)


def test_no_agent_can_open_a_pull_request():
    import agent

    for a in (agent.diagnostic_agent, agent.mitigation_agent, agent.verifier_agent, agent.reporter_agent):
        assert "open_pull_request" not in _tool_names(a)


# ------------------------------------------------------------- HTTP API ----

@pytest.fixture
def client():
    import app

    app.GREEN_TEAM_TOKEN = "test-token"
    return app.app.test_client()


AUTH = {"Authorization": "Bearer test-token"}


def test_api_requires_bearer_token(client):
    assert client.get("/healthz").status_code == 200
    assert client.post("/chat", json={"message": "hi"}).status_code == 401
    assert client.post("/approvals/inc-1", json={"decision": "approve"},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_reject_is_recorded_without_any_llm_call(client):
    state.create_incident("inc-5", title="t", service="warden")
    r = client.post("/approvals/inc-5", json={"decision": "reject", "approver": "sean"}, headers=AUTH)
    assert r.get_json() == {"status": "rejected"}
    assert state.get_incident("inc-5")["status"] == "rejected"
    assert ("rejected", "sean") in events("inc-5")


def test_approve_opens_pr_directly(client, monkeypatch):
    import app

    calls = []
    monkeypatch.setattr(app, "open_pull_request", lambda iid: calls.append(iid) or {"html_url": "https://example/pr/1"})
    state.create_incident("inc-6", title="t", service="warden")
    r = client.post("/approvals/inc-6", json={"decision": "approve", "approver": "sean"}, headers=AUTH)
    assert r.get_json() == {"status": "executing", "pr_url": "https://example/pr/1"}
    assert calls == ["inc-6"]
    assert ("approved", "sean") in events("inc-6")


def test_bad_decision_is_rejected(client):
    assert client.post("/approvals/inc-7", json={"decision": "maybe"}, headers=AUTH).status_code == 400
