"""Runs Warden's offline smoke test (fake Firestore + fake Gemini) and checks
the security-relevant outcomes: auth, role checks enforced by the API layer
(not the LLM), and canary-leak detection."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent


def test_warden_smoke():
    env = dict(os.environ, GCP_PROJECT="local")
    proc = subprocess.run(
        [sys.executable, "local_test.py"], cwd=SERVICE_DIR, env=env,
        capture_output=True, text=True, timeout=60,
    )
    out = proc.stdout
    assert proc.returncode == 0, proc.stderr
    assert "login_alice -> (200" in out
    assert "login_bad -> (401" in out
    assert "secret_as_user -> (403" in out                     # API layer enforces the real role
    assert "chat_alice_secret_attempt -> (200, {'reply': \"Here is what I found: {'error': 'forbidden'}" in out
    assert "secret_leaked" in out                             # outbound canary scan fires
    assert "chat_unauth -> (401" in out
