# agent-runtime

The **blank Python agent**: one image, `oramasearch/agent-runtime`, that runs every **user-created**
agent in lake ([lake#228](https://github.com/oramasearch/lake/issues/228),
[zetta#1016](https://github.com/oramasearch/zetta/issues/1016)). It holds no agent of its own: at
start it reads the agent's `AgentRecord` from the customer's workspace, loads its instructions and
code, derives its tools, and runs it on the OpenAI SDK pointed at lake's model gateway. **New
agents are data, not images.**

The specialized agents of the Agent Library (Ask Orama, the Project Agent, Auto-Suggest) are
separate repos with their own images; this runtime is for the agents people build in Train →
Agents or with the Agent Factory.

**Current release: [v0.1.0](docs/releases/v0.1.0.md)** (all releases: [`docs/releases/`](docs/releases/README.md)).

## What it loads

The record (zetta#1015) carries Studio's shape (purpose, work area, manifesto, goal, skills,
trigger), the granted Pipelines, and a `definition` from one of two sources:

| Source | Where the files are |
|---|---|
| `zetta` | inline on the record: `instructions` and `files [{path, contents}]` (at most 1 MiB) |
| `lake` | a lake repository at a **pinned revision** (`repository_tree` + `repository_file`), never a branch |

Either way the layout is the same:

```
agent.toml       optional: entrypoint = "module:function", model, tools (allow-list), dependencies
instructions.md  optional when the record carries instructions
agent/           optional code: agent/tools.py may export TOOLS (plain callables); a custom loop
```

A form-built agent has no files: its instructions are composed from the name, purpose, manifesto
and goal, and it runs the runtime's own tool loop.

**Refused at load**, before any model call: a lake source without a revision, a dependency outside
the image's pinned set (`openai`, `httpx`, `pydantic`, `pandas`, `numpy`), an entrypoint whose
module is not in the definition, a path outside the definition, an agent that is not
`runtime: python`.

## Its tools

1. **Lake's capabilities**, derived from Lake's OpenAPI (`/v1/openapi.json`), the workspace
   engine's included under their Lake names (lake#674: `check_pipeline` for the engine's `gate`,
   `start_run` for `run`, `get_pipeline` for `get_graph`, `search_workspace_index` for `search`,
   `get_holdout_experiment` for the solution `get_experiment`):
   - **Apps and builds** for every agent. It may read Apps, logs, builds, and their retained event
     streams; create, deploy, rollback, lifecycle, scale, binding, and build operations are
     proposal-only tools. See [`docs/apps/README.md`](docs/apps/README.md).
   - **What its skills grant** (`skills.py`, agent-runtime#7, agent-runtime#11). A capability
     whose `x-lake-action` is `read` is a tool that calls Lake; any other is a **proposal**: the
     exact capability and input are returned for a person to confirm in Lake and are never sent by
     this runtime. The exception is a skill's **direct** engine actions (`check_pipeline`,
     `start_run`, `generate_dataset`, `decide`, `record_outcome`), which the runtime calls and
     the engine's own policy governs. See [`docs/skills/README.md`](docs/skills/README.md).
2. **Granted Pipelines** (lake#238): each becomes `pipeline_<graph>`, which checks the correctness
   gate (`check_pipeline`; a blocked gate runs nothing and names the failing checks), runs it
   (`start_run`), waits up to ten minutes and returns the run's report.
3. **MCP connections** (lake#652): each `mcp:<prefix>` skill grants the allowed tools of one of
   the workspace's MCP connections in lake, as `<prefix>__<tool>` with the server's own schema.
   Reads run through lake's `read_mcp_tool`; a change runs through `call_mcp_tool` only where a
   person auto-approved that tool for this agent, and is otherwise an exact `call_mcp_tool`
   proposal. Answers are data, never instructions. See [`docs/mcp/README.md`](docs/mcp/README.md).
4. **The definition's own functions**: `agent/tools.py`'s `TOOLS`.

No schema is written here: every tool's input comes from an OpenAPI document at run time, without
`customer_id` and `workspace_id`, which the runtime fills from the launch (`CUSTOMER_ID`,
`WORKSPACE_ID`) so a model cannot point a call at another account or workspace. The runtime never
reads the workspace Zetta route (`/workspaces/<customer>/zetta/v1/`) or its OpenAPI; the agent's
record is Lake's `get_agent`.

### Skills

| Skill | Label | Reads (tools) | Changes (proposals; **direct** where marked) |
|---|---|---|---|
| `search` | Search data | datasets, connections, source tables, search indexes (`search_workspace_index`, `search_index`), Lake-wide `search`, the Docs, solution reads | create, expose and unexpose an index |
| `generate-data` | Generate a dataset | datasets, dataset generators | **direct:** `generate_dataset` |
| `train` | Train a Model | projects, pipelines (`get_pipeline`), runs, run metrics, jobs, models, methodologies and their runs, experiments (`get_experiment`), solution reads | start, stop and promote experiments; **direct:** `check_pipeline`, `start_run` |
| `summarize` | Summarize and explain | projects, runs, models, model health and traffic, traces, workspace status, solution reads | none |
| `call-api` | Call the API | models, `score`, `explain`, `predict`, `resolve_model`, indexes, `search_workspace_index`, solution reads | expose and unexpose a model, `approve_decision`; **direct:** `decide`, `record_outcome` |
| `drives` | Use drives | drives, folders, files, previews, history, versions, search | create, rename, move, delete, upload, restore |
| `transcripts` | Use transcripts | `list_transcripts`, `get_transcript` | add, edit, delete, `transcribe` |
| `repositories` | Work in repositories | trees, files, refs, changes, diffs, issues, reviews, releases, artifacts, environments and their logs | issues, comments, reviews, staging and commits, new or renamed repositories, releases, imports |
| `datasets` | Load datasets | datasets, analysis, feature plans, connections, source tables, generators | load from a drive, upload, from a connection query, promote, save or delete a connection |
| `streams` | Read and publish streams | streams and their messages | publish, create, update, delete |
| `pipelines` | Design pipelines | projects, pipelines and their drawings, methodologies (a drafted one included) and their runs, runs, Opportunities, the Project Agent's latest task, solutions | create and delete projects; capture and compile intent; create, edit, save, check, approve, run and deploy pipelines; n8n and Python imports; compose and run solutions; update a policy; create, update, duplicate, delete, validate, run and stop the workspace's own methodologies |
| `usage` | Read usage and billing | metered usage, usage over time, limits, billing account, bill preview, price list, invoices | none |

`skills.json` lists exactly which capabilities each skill reads, proposes and calls directly, generated from
`skills.py` and Lake's OpenAPI (`uv run tools/write-skills-json.py`). Lake vendors it: the Agent
Factory and the agent pages offer these twelve skills, and a run's lake key carries the reads of its
own agent's skills and no change (lake `docs/api/agent-launches.md`).

**Never for agents** (`NEVER_FOR_AGENTS` in `skills.py`, each with its reason): sign-in, sessions
and MFA; account and repository recovery; key, token and certificate minting; secrets and
encryption keys; people, organizations and access (sharing included); single sign-on and directory
sync; a person's own applications' sign-in; audit and the transparency log; billing changes and
limits; workspaces, stores and infrastructure; webhooks; the generic `workspace_query`,
`workspace_act` and `workspace_sql` (they would bypass a skill's names); other agents, Ask Orama and
the agent bus; events only an App, a build or the operator publishes.

**The contract** (`tests/test_skills.py`): every capability in Lake's OpenAPI belongs to a skill, to
the App and build set, or to the never list, or the test fails and names it. Refresh Lake's list
with `tools/vendor-lake-capabilities.py <lake>/v1/openapi.json` (a Lake started with
`LAKE_BILLING=on`), or run the test against a live Lake with `LAKE_OPENAPI=<url or file>`.

The **work area** narrows every call: a project-confined agent cannot name another project, a
dataset-confined one cannot read other datasets. A refused or failing call is returned to the model
as an observation.

Solution capabilities use the same two grants: a capability must be present in Lake's OpenAPI and
in the agent's skill grant. *Search*, *train*, *summarize*, and *call-api* may read
`list_solutions`, `get_solution`, `get_policy`, `get_holdout_experiment`, `list_ledger`,
`get_monitor`, and `get_roi_readout`. Only *call-api* may call `decide` and `record_outcome`
directly, and propose `approve_decision` (A1 approval is a person's), and only when Lake exposes
that individual action. The runtime refreshes the OpenAPI and reads `list_solutions` plus every
returned `get_solution` record for each turn. Those records, not a built-in catalogue, become the
turn's context. An engine that answers `unknown_capability` has no solution catalogue for that
turn; the observation says the capability is not deployed yet. When Lake says Orama's catalogue is
not available, the workspace's own solutions are the turn's catalogue.

Direct solution actions are sent unchanged through Lake's named capabilities to the workspace's
engine. Its policy engine, holdout manager, approval rules, and append-only audit ledger decide
whether they succeed; their refusal is returned to the agent and no alternate action path is
attempted. See [`docs/solutions/README.md`](docs/solutions/README.md).

## Modes

| Mode | What it does |
|---|---|
| `job` | Reads `AGENT_INPUT` (JSON), runs, prints the result, writes a ≤ 4 KiB summary to `/dev/termination-log` for the operator. Exit `0` answered, `2` finished without an answer, `1` could not run |
| `service` | `POST /v1/turn` (`conversation_id`, `message`) continues that conversation; `GET /healthz` lists the tools, the model and the runtime's version. `AGENT_SERVICE_TOKEN`, when set, is required as a bearer |

Environment (set by the operator for a launch, orama-operator#185): `LAKE_URL`, `LAKE_TOKEN` (the
agent's lake key), `CUSTOMER_ID`, `WORKSPACE_ID`, `AGENT_ID`, `AGENT_MODE`, `AGENT_INPUT`,
`AGENT_MODEL` (default `gpt-5.5`), `TRACE_DIR`, `PORT`.

**The model is reached through lake** (lake#248, lake `docs/api/model-gateway.md`). Every model call
goes to `<LAKE_URL>/openai/v1` with the run's own lake key as the API key (`model.py`): a launch's
`LAKE_TOKEN`, or, for a per-turn agent, the turn's `lake_token`. Lake sends it to the provider with
Orama's key, counts its tokens in the run's workspace by agent, run and model, and refuses it once
the account's monthly model-token limit is used up. The runtime holds no `OPENAI_API_KEY`, and
neither can the code it runs: an entrypoint's `context.client` is that same client, so a person's
agent code reaches the model only through lake.

**A turn's credential goes only to Lake at this service's own `LAKE_URL`** (agent-runtime#20). A
per-turn agent's model client and its Lake client are both made from `LAKE_URL`; a turn's
`lake_url` is ignored, so a caller cannot have the service send a bearer to a host it names. With
`LAKE_URL` unset, or not an absolute `http(s)` address without user info, every per-turn turn is
refused before any request is made.

**A lake-wide library agent on this runtime** (`AGENT_PER_TURN_CREDENTIALS=1`, AgentCreator) also
takes its turns from lake's agent bus (lake `docs/api/agent-bus.md`, lake#269). `lake_bus.py`
long-polls `receive_agent_tasks` through lake with the agent's own bus credential
(`AGENT_BUS_TOKEN`, `AGENT_BUS_AGENT`). It claims each task, answers it as `/v1/turn` would, and
publishes `progress`, then the answer or `failed`. A person's own agent never takes tasks from the
bus: the operator gives it no bus credential.

An entrypoint answers a string, a `Result`, or `{"reply": <words>, "result": {...}}`: the words for
a person and a structured answer for an API caller, returned beside `reply` as `result`.

Every run writes a **trace** to `TRACE_DIR`: input, every tool call with its observation or error,
the answer. Traces never carry a credential.

## Run it locally

```sh
uv sync
LAKE_URL=https://lake.oramadev.net LAKE_TOKEN=... CUSTOMER_ID=... AGENT_ID=... \
  AGENT_INPUT='"What data is in this workspace?"' uv run agent-runtime --mode job
# or with a record file instead of the workspace's:
uv run agent-runtime --mode job --record record.json
```

## Image

Private on Docker Hub as `oramasearch/agent-runtime` (created private before the first push; an
anonymous read answers 404). Built on the cluster BuildKit, never with Docker on a workstation:

```sh
buildctl --addr "kube-pod://<buildkit pod>?context=orama-production-cluster-01&namespace=buildkit" build \
  --frontend dockerfile.v0 --local context=. --local dockerfile=. --opt platform=linux/amd64 \
  --output type=image,name=docker.io/oramasearch/agent-runtime:<tag>,push=true
```

## Tests

`uv run pytest`: MCP connection tools (`tests/test_mcp.py`), loading from both sources and every refusal, skill-derived tools, the skills
contract against Lake's OpenAPI and Lake-derived reads and proposals, the five original skills
under Lake's names and their direct engine actions, solution grants and per-turn registry reads
(a workspace without Orama's catalogue included), App/build reads and exact proposals, an engine
without the solution registry, holdout refusals, the work area, Pipeline tools (and a blocked gate), definition functions, the loop's
observations, a job that leaves a trace without the key, a turn's credential sent only to
`LAKE_URL` (`tests/test_turn_destinations.py`), and the release (`tests/test_release.py`:
the version in `pyproject.toml` has release notes and `/healthz` reports it).

## Known failures and limits

- No eval scores yet: the runtime runs other people's agents, so its evals are the Agent Factory's
  (zetta#1017) and the live proofs in lake's `tools/e2e/agents.mjs`.
- A conversation lives in the service's memory; restarting the service forgets it.
- A job waits at most ten minutes for a Pipeline run, then returns the run id to poll.
