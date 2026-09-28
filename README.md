# agent-runtime

The **blank Python agent**: one image, `oramasearch/agent-runtime`, that runs every **user-created**
agent in lake ([lake#228](https://github.com/oramasearch/lake/issues/228),
[zetta#1016](https://github.com/oramasearch/zetta/issues/1016)). It holds no agent of its own: at
start it reads the agent's `AgentRecord` from the customer's workspace, loads its instructions and
code, derives its tools, and runs it on the OpenAI SDK. **New agents are data, not images.**

The specialized agents of the Agent Library (Ask Orama, the Project Agent, Auto-Suggest) are
separate repos with their own images; this runtime is for the agents people build in Train →
Agents or with the Agent Factory.

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

1. **The workspace's Zetta capabilities**, derived from lake's filtered OpenAPI
   (`/workspaces/<customer>/zetta/v1/openapi.json`, lake#235) and kept only where the agent's
   **skills** grant them (`SKILL_CAPABILITIES` in `tools.py`): *search*, *generate-data*, *train*,
   *summarize*, *call-api*.
2. **Granted Pipelines** (lake#238): each becomes `pipeline_<graph>`, which checks the correctness
   gate (a blocked gate runs nothing and names the failing checks), runs it, waits up to ten minutes
   and returns the run's report.
3. **The definition's own functions**: `agent/tools.py`'s `TOOLS`.

The **work area** narrows every call: a project-confined agent cannot name another project, a
dataset-confined one cannot read other datasets. A refused or failing call is returned to the model
as an observation.

Solution capabilities use the same two grants: a capability must be present in lake's filtered
OpenAPI and in the agent's skill grant. *Search*, *train*, *summarize*, and *call-api* may read
`list_solutions`, `get_solution`, `get_policy`, `get_experiment`, `list_ledger`, `get_monitor`, and
`get_roi_readout`. Only *call-api* may receive `decide`, `record_action`, `record_outcome`, and
`approve_decision`, and only when lake exposes that individual action. The runtime refreshes the
OpenAPI and reads `list_solutions` plus every returned `get_solution` record for each turn. Those
records, not a built-in catalogue, become the turn's context. An engine that answers
`unknown_capability` has no solution catalogue for that turn; the observation says the capability
is not deployed yet.

Solution actions are sent unchanged through lake to Zetta. Zetta's policy engine, holdout manager,
approval rules, and append-only audit ledger decide whether they succeed; their refusal is returned
to the agent and no alternate action path is attempted. See
[`docs/solutions/README.md`](docs/solutions/README.md).

## Modes

| Mode | What it does |
|---|---|
| `job` | Reads `AGENT_INPUT` (JSON), runs, prints the result, writes a ≤ 4 KiB summary to `/dev/termination-log` for the operator. Exit `0` answered, `2` finished without an answer, `1` could not run |
| `service` | `POST /v1/turn` (`conversation_id`, `message`) continues that conversation; `GET /healthz` lists the tools and model. `AGENT_SERVICE_TOKEN`, when set, is required as a bearer |

Environment (set by the operator for a launch, orama-operator#185): `LAKE_URL`, `LAKE_TOKEN` (the
agent's lake key), `CUSTOMER_ID`, `AGENT_ID`, `AGENT_MODE`, `AGENT_INPUT`, `OPENAI_API_KEY` (the
general Orama key), `AGENT_MODEL` (default `gpt-5.5`), `TRACE_DIR`, `PORT`.

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

`uv run pytest`: loading from both sources and every refusal, skill-derived tools, solution grants
and per-turn registry reads, legacy-engine behavior, holdout refusals, the work area, Pipeline tools
(and a blocked gate), definition functions, the loop's observations, and a job that leaves a trace
without the key.

## Known failures and limits

- No eval scores yet: the runtime runs other people's agents, so its evals are the Agent Factory's
  (zetta#1017) and the live proofs in lake's `tools/e2e/agents.mjs`.
- A conversation lives in the service's memory; restarting the service forgets it.
- A job waits at most ten minutes for a Pipeline run, then returns the run id to poll.
