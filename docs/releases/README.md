# Releases

One page per tagged release, newest first. A page states what the release includes, what it does
not include, known issues, verification output, and where the image runs. The first release lists
everything the runtime does: its modes, how it loads an agent, its tools and skills, its HTTP API,
its environment and its image.

A release has one `vX.Y.Z` name shared by the Git tag, the GitHub release and the Docker Hub
image tag `oramasearch/agent-runtime:vX.Y.Z`. The image tag names the digest already built from the
tag and verified; it is never produced by a second build.

| Release | Date | Summary |
|---|---|---|
| [v0.2.0](v0.2.0.md) | 2026-10-25 | A minor release of security fixes: the service token compared in constant time and required, a turn's credential sent only to `LAKE_URL`, and dataset-confined agents limited to reads that can be proven inside their dataset |
| [v0.1.0](v0.1.0.md) | 2026-10-17 | The first release: the job and service modes, loading an agent from Zetta, a Lake repository or the image, tools derived from Lake's OpenAPI and twelve skills plus MCP connections, the model through Lake's gateway, AgentCreator's agent-bus client, and the image |

`pyproject.toml`'s `[project].version` is the one place the version is set; `GET /healthz` reports
it, and `tests/test_release.py` fails when that version has no page here.
