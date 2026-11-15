# SPDX-License-Identifier: MIT

"""The release this tree is: its version, read from the code, has release notes, and the running
service reports it on `GET /healthz`. Never repeat the number here."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

from agent_runtime import definition, runner

ROOT = Path(__file__).resolve().parent.parent
RELEASES = ROOT / "docs" / "releases"


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_the_installed_package_is_the_project_version():
    assert importlib.metadata.version("agent-runtime") == project_version()


def test_release_notes_exist_for_the_project_version():
    version = project_version()
    notes = RELEASES / f"v{version}.md"
    assert notes.exists(), f"no release notes at {notes.relative_to(ROOT)}"
    first_heading = next(line for line in notes.read_text().splitlines() if line.startswith("# "))
    assert f"v{version}" in first_heading
    assert f"(v{version}.md)" in (RELEASES / "README.md").read_text()


def test_healthz_reports_the_runtime_version():
    loaded = runner.Loaded(definition.Agent(id="agent-1", name="Briefer"), [], "gpt-5.5")
    body = runner.health(loaded)
    assert body["ok"] is True
    assert body["version"] == project_version()
    assert body["agent"] == "agent-1" and body["tools"] == [] and body["model"] == "gpt-5.5"
