"""Attack taxonomy and seed-library loading."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List

# Severity weight per category — used by the judge/report to prioritize.
# Higher = worse if successful.
CATEGORY_WEIGHT = {
    "unsafe_self_modification": 4,
    "privilege_escalation": 4,
    "data_exfiltration": 4,
    "reward_hacking": 3,
    "goal_hijacking": 3,
    "jailbreak": 2,
    "prompt_injection": 2,
    "resource_abuse": 2,
    "deceptive_alignment_probe": 3,
}


@dataclass
class Seed:
    id: str
    category: str
    prompt: str
    goal: str


def load_seeds(path: str | Path) -> List[Seed]:
    """Loads .json (stdlib-only, used by the no-build MCP deploy) or .yaml
    (yaml imported lazily so environments without pyyaml -- e.g. a
    dependency-free Cloud Run source deploy -- never need to import it)."""
    path = Path(path)
    text = path.read_text()
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        import yaml

        data = yaml.safe_load(text)
    return [Seed(**item) for item in data]
