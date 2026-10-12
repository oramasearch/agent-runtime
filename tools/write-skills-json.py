#!/usr/bin/env python3
"""Write skills.json: each skill with its label and the Lake capabilities it grants, split into
reads (tools) and changes by Lake's own `x-lake-action` (tests/fixtures/lake-capabilities.json,
vendored from Lake's OpenAPI). A change is a proposal, except the skill's `direct` engine actions,
which the runtime calls itself under the engine's policy (agent-runtime#11).

Lake vendors this file (lake `tools/agents/vendor-agent-skills.py`): the Agent Factory and the
agent pages offer exactly these skills, and a launched agent's key carries the reads of its own
skills and nothing more. tests/test_skills.py fails when the file is out of date.

    uv run tools/write-skills-json.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_runtime.skills import SKILLS, never_for_agents

FIXTURE = ROOT / "tests" / "fixtures" / "lake-capabilities.json"
OUTPUT = ROOT / "skills.json"


def document() -> str:
    capabilities = json.loads(FIXTURE.read_text())["capabilities"]
    never = never_for_agents()
    skills = []
    for skill in SKILLS.values():
        granted = sorted(name for name in skill.lake if name in capabilities and name not in never)
        skills.append({
            "name": skill.name,
            "label": skill.label,
            "reads": [name for name in granted if capabilities[name]["action"] == "read"],
            "proposals": [name for name in granted
                          if capabilities[name]["action"] != "read" and name not in skill.direct],
            "direct": [name for name in granted
                       if capabilities[name]["action"] != "read" and name in skill.direct],
        })
    return json.dumps({"skills": skills}, indent=1) + "\n"


def main() -> int:
    OUTPUT.write_text(document())
    print(f"{len(SKILLS)} skills -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
