# CLAUDE.md — agent-runtime

The blank Python agent: the runtime that loads a user-created agent definition at start and runs its
turns against Lake. Read `README.md` and the folder `README.md` files under `docs/` first.

## Rules specific to this repo

- **It runs only user-created agents.** The specialized agents (Ask Orama, `agent-project`,
  `agent-autosuggest`) have their own repositories and images. Never make them definitions here.
- **No agent gets an image.** An agent is a definition loaded at start. A dependency outside
  `ALLOWED_DEPENDENCIES` (`definition.py`) is refused at load, never installed at run time; adding
  one is a runtime release.
- **Everything goes through Lake.** The workspace engine through Lake's named capabilities
  (`/v1/<name>`; never the `/workspaces/<customer>/zetta/v1/` route), code through
  `repository_tree` / `repository_file`. The runtime never holds a workspace's admin token.
- **Tools are derived**, from Lake's filtered OpenAPI and the agent's skills, never hand-written.
  App and build reads are available without a Studio skill; App and build changes are exact
  proposals for a person to confirm, never writes from this runtime.
- **Every Lake capability is accounted for.** It belongs to a skill, to the App and build set, or
  to `NEVER_FOR_AGENTS` (`skills.py`); `tests/test_skills.py` fails otherwise. A skill's Lake reads
  are tools and its changes are proposals, except its `direct` engine actions, which the engine's
  policy governs (`docs/skills/README.md`, *Direct actions*: adding one is documented there with its
  reason). `skills.json` is regenerated with the skills and vendored by Lake
  (`docs/skills/README.md`).
- **A failing tool is an observation**, never an exception that ends the run.
- **Every run is a trace**, failures included, without any credential.

## Build and test

- Python with uv, never pip: `uv sync`, then `uv run --group dev pytest`.

## Conventions

- Every change documents itself (the `docs/<topic>/` page, or `README.md`) **in the same commit**.
- **No AI attribution anywhere**: no AI `Co-Authored-By` trailer, no "Generated with", no session or
  model trailers, in commits, pull requests, tags or release notes.
- Conventional Commits; a feature branch and a pull request, never a push to the default branch.
- No time or size estimates.
- The word "seam" is banned. Name the mechanism: interface, extension point, boundary, injected
  dependency.
