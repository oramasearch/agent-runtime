"""The skills contract: every Lake capability is in a skill, every agent's App set, or the
never-for-agents list, and a skill's tools are derived from Lake's OpenAPI (agent-runtime#7).

The capability list is Lake's own OpenAPI, vendored by tools/vendor-lake-capabilities.py into
tests/fixtures/lake-capabilities.json. Set LAKE_OPENAPI to a running Lake's
`/v1/openapi.json` (or a saved copy) to check the contract against that Lake instead.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent_runtime import apps, definition, tools
from agent_runtime.lake import Lake
from agent_runtime.loop import run_loop, system_message
from agent_runtime.skills import NEVER_FOR_AGENTS, SKILLS, never_for_agents

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "lake-capabilities.json"
CUSTOMER = "cust-1"
WORKSPACE = "ws-1"


def lake_capabilities() -> dict[str, dict]:
    source = os.environ.get("LAKE_OPENAPI")
    if not source:
        return json.loads(FIXTURE.read_text())["capabilities"]
    spec = importlib.util.spec_from_file_location(
        "vendor", ROOT / "tools" / "vendor-lake-capabilities.py")
    vendor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vendor)
    return vendor.reduce(vendor.load(source))["capabilities"]


CAPABILITIES = lake_capabilities()


def skill_grants() -> set[str]:
    granted: set[str] = set()
    for skill in SKILLS.values():
        granted |= skill.lake
    return granted


# -- the contract --------------------------------------------------------------------------------

def test_every_lake_capability_belongs_to_a_skill_or_is_never_for_agents():
    accounted = skill_grants() | apps.APP_CAPABILITIES | never_for_agents()
    unaccounted = sorted(set(CAPABILITIES) - accounted)
    assert not unaccounted, (
        "Lake publishes capabilities no skill grants and the never-for-agents list does not "
        f"name: {unaccounted}. Add each to a skill in skills.py, or to NEVER_FOR_AGENTS with "
        "its reason.")


def test_no_skill_names_a_capability_lake_does_not_publish():
    named = skill_grants() | apps.APP_CAPABILITIES | never_for_agents()
    assert sorted(named - set(CAPABILITIES)) == []


def test_nothing_is_both_granted_and_never_for_agents():
    never = never_for_agents()
    assert sorted(skill_grants() & never) == []
    assert sorted(apps.APP_CAPABILITIES & never) == []
    groups = list(NEVER_FOR_AGENTS.values())
    for index, group in enumerate(groups):
        for other in groups[index + 1:]:
            assert not group & other


def test_sign_in_recovery_key_minting_and_secret_reads_are_never_for_agents():
    never = never_for_agents()
    for name in ["sign_in_password", "sign_in_passkey", "start_oauth", "begin_email_recovery",
                 "complete_recovery", "recover_repository_key", "create_token",
                 "mint_agent_session_key", "issue_agent_certificate", "issue_x509_svid",
                 "read_secret", "mint_sealed_secret", "list_secrets", "workspace_query",
                 "workspace_act"]:
        assert name in never, name
        assert name in CAPABILITIES, name


@pytest.mark.parametrize("skill, capabilities", [
    ("drives", ["drive_browse", "read_drive_file", "search_drives", "upload_drive_files"]),
    ("transcripts", ["list_transcripts", "get_transcript", "create_transcript", "transcribe"]),
    ("search", ["search", "search_workspace_index"]),
    ("repositories", ["repository_tree", "repository_file", "list_issues", "create_issue"]),
    ("datasets", ["load_drive_dataset", "create_dataset_from_query", "upload_dataset"]),
    ("streams", ["browse_stream", "publish_stream_message"]),
    ("pipelines", ["create_pipeline", "edit_pipeline", "run_pipeline", "create_project"]),
    ("usage", ["metering_usage", "usage_over_time", "usage_limits", "billing_account"]),
])
def test_each_area_of_lake_has_a_skill(skill, capabilities):
    for name in capabilities:
        assert name in SKILLS[skill].lake, name
        assert name in CAPABILITIES, name


def test_reads_and_changes_come_from_lakes_own_statement():
    reads = {name for name, operation in CAPABILITIES.items() if operation["action"] == "read"}
    assert "drive_browse" in reads and "list_transcripts" in reads
    assert "publish_stream_message" not in reads and "create_pipeline" not in reads
    # An action that only reads (sent as POST) is still a read.
    assert CAPABILITIES["predict"]["method"] == "post" and "predict" in reads


# -- tools derived from Lake's OpenAPI ------------------------------------------------------------

def openapi_for(names: list[str], both_methods: tuple[str, ...] = ()) -> dict:
    """Lake's OpenAPI for some capabilities, rebuilt from the vendored one."""
    paths: dict = {}
    for name in names:
        capability = CAPABILITIES[name]
        operation = {"operationId": name, "summary": name, "description": f"Lake's {name}.",
                     "tags": ["read" if capability["action"] == "read" else "action"],
                     "x-lake-action": capability["action"]}
        get = {**operation, "parameters": [
            {"in": "query", "name": field, "required": field == "customer_id",
             "schema": {"type": "string"}} for field in capability["inputs"]]}
        post = {**operation, "requestBody": {"content": {"application/json": {"schema": {
            "type": "object",
            "properties": {field: {"type": "string"} for field in capability["inputs"]},
            "required": [field for field in capability["inputs"] if field == "customer_id"]}}}}}
        if capability["method"] == "get":
            paths[f"/v1/{name}"] = {"get": get, **({"post": post} if name in both_methods else {})}
        else:
            paths[f"/v1/{name}"] = {"post": post}
    return {"openapi": "3.1.0", "paths": paths}


class FakeLake:
    def __init__(self, lake_openapi: dict) -> None:
        self.requests: list[httpx.Request] = []
        self.lake_openapi = lake_openapi

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/openapi.json":
            return httpx.Response(200, json=self.lake_openapi)
        return httpx.Response(200, json={"ok": True, "value": {"path": path,
                                                               **dict(request.url.params)}})


def agent_with(skills: list[str], tmp_path, **record) -> definition.Agent:
    return definition.from_record({
        "id": "a-1", "name": "Helper", "runtime": "python", "skills": skills,
        "definition": {"source": "zetta", "instructions": "Help.", "files": []}, **record,
    }, workdir=tmp_path)


def lake_for(fake: FakeLake) -> Lake:
    return Lake("http://lake/w/" + WORKSPACE, "agent-token", CUSTOMER,
                transport=httpx.MockTransport(fake), workspace_id=WORKSPACE)


AREA_NAMES = ["drive_browse", "read_drive_file", "upload_drive_files", "list_transcripts",
              "create_transcript", "publish_stream_message", "browse_stream", "metering_usage",
              "read_secret", "create_token"]


def test_a_skill_grants_its_reads_as_tools_and_its_changes_as_proposals(tmp_path):
    openapi = openapi_for(AREA_NAMES)
    lake = lake_for(FakeLake(openapi))

    none = tools.all_tools(agent_with([], tmp_path / "none"), lake, openapi)
    assert {tool.name for tool in none} == set()

    drives = {tool.name: tool for tool in tools.all_tools(
        agent_with(["drives"], tmp_path / "drives"), lake, openapi)}
    assert set(drives) == {"drive_browse", "read_drive_file", "upload_drive_files"}
    assert not drives["drive_browse"].proposal and not drives["read_drive_file"].proposal
    assert drives["upload_drive_files"].proposal
    assert drives["upload_drive_files"].description.startswith("Propose this lake change")

    everything = tools.all_tools(agent_with(list(SKILLS), tmp_path / "all"), lake,
                                 openapi)
    names = {tool.name for tool in everything}
    assert "read_secret" not in names and "create_token" not in names
    assert {"list_transcripts", "create_transcript", "browse_stream", "publish_stream_message",
            "metering_usage"} <= names


def test_a_read_calls_lake_as_the_runs_customer_and_workspace(tmp_path):
    openapi = openapi_for(["browse_stream", "drive_browse"])
    fake = FakeLake(openapi)
    by_name = {tool.name: tool for tool in tools.all_tools(
        agent_with(["streams", "drives"], tmp_path), lake_for(fake), openapi)}

    for name in ["browse_stream", "drive_browse"]:
        properties = by_name[name].parameters["properties"]
        assert "customer_id" not in properties and "workspace_id" not in properties

    browsed = by_name["browse_stream"].call({"stream": "orders", "customer_id": "other"})
    assert browsed.ok
    sent = fake.requests[-1]
    assert sent.url.path == f"/w/{WORKSPACE}/v1/browse_stream"
    assert sent.url.params["customer_id"] == CUSTOMER
    assert sent.url.params["workspace_id"] == WORKSPACE

    # drive_browse names a repository, not a customer: nothing is added that it does not take.
    by_name["drive_browse"].call({"repository_id": "r-1", "ref": "main"})
    sent = fake.requests[-1]
    assert sent.url.path == f"/w/{WORKSPACE}/v1/drive_browse"
    assert "customer_id" not in sent.url.params
    assert sent.url.params["repository_id"] == "r-1"


def test_a_read_lake_takes_on_get_and_post_is_one_tool(tmp_path):
    openapi = openapi_for(["list_streams"], both_methods=("list_streams",))
    derived = tools.all_tools(agent_with(["streams"], tmp_path), lake_for(FakeLake(openapi)),
                              openapi)
    assert [tool.name for tool in derived] == ["list_streams"]


def test_a_skills_changes_are_proposals_that_never_reach_lake(tmp_path):
    openapi = openapi_for(["create_pipeline", "get_project"])
    fake = FakeLake(openapi)
    agent = agent_with(["pipelines"], tmp_path)
    derived = tools.all_tools(agent, lake_for(fake), openapi)
    call = SimpleNamespace(id="c-1", function=SimpleNamespace(
        name="create_pipeline", arguments=json.dumps({"project": "churn", "from": "n8n"})))
    replies = iter([
        SimpleNamespace(content=None, tool_calls=[call],
                        model_dump=lambda **_: {"role": "assistant", "content": None}),
        SimpleNamespace(content="I proposed the pipeline.", tool_calls=None,
                        model_dump=lambda **_: {"role": "assistant",
                                                "content": "I proposed the pipeline."}),
    ])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_: SimpleNamespace(choices=[SimpleNamespace(message=next(replies))]))))
    messages = [system_message(agent)]

    result = run_loop(agent, derived, messages, client, "gpt-5.5")

    assert result.proposals == [{"capability": "create_pipeline",
                                 "input": {"project": "churn", "from": "n8n"},
                                 "impact": "Lake's create_pipeline."}]
    assert result.calls[0]["observation"]["status"] == "proposed"
    assert not any(request.url.path.endswith("/v1/create_pipeline")
                   for request in fake.requests)
    assert "proposed, never that it is done" in messages[0]["content"]


def test_the_work_area_narrows_lake_reads(tmp_path):
    openapi = openapi_for(["get_project"])
    fake = FakeLake(openapi)
    agent = agent_with(["pipelines"], tmp_path,
                       work_area={"kind": "project", "detail": "churn"})
    tool = {tool.name: tool for tool in tools.all_tools(
        agent, lake_for(fake), openapi)}["get_project"]

    refused = tool.call({"project": "pricing"})
    assert not refused.ok and "only in project 'churn'" in refused.error
    assert tool.call({"project": "churn"}).ok


# -- the five original skills, under Lake's names (agent-runtime#11) ------------------------------

ORIGINAL_FIVE = ["search", "generate-data", "train", "summarize", "call-api"]

# What each original skill reached on the workspace engine under the engine's name, and the Lake
# capability that does the same job (lake#674, lake docs/api/engine-capabilities.md).
ENGINE_TO_LAKE = {
    "search": "search_workspace_index", "get_graph": "get_pipeline", "gate": "check_pipeline",
    "run": "start_run", "get_experiment": "get_holdout_experiment",
}
ENGINE_GRANTS_BEFORE = {
    "search": {"list_datasets", "get_dataset", "list_connections", "list_source_tables",
               "list_indexes", "get_index", "search"},
    "generate-data": {"list_datasets", "list_dataset_generators", "generate_dataset"},
    "train": {"list_datasets", "get_dataset", "list_projects", "get_project", "get_graph",
              "list_runs", "get_run", "get_run_report", "list_models", "get_model", "gate", "run"},
    "summarize": {"list_projects", "get_project", "list_runs", "get_run", "get_run_report",
                  "list_models", "get_model", "model_health"},
    "call-api": {"list_models", "get_model", "score", "explain", "list_indexes", "search",
                 "decide", "record_outcome", "approve_decision"},
}
ENGINE_SOLUTION_READS = {"list_solutions", "get_solution", "get_policy", "list_ledger",
                         "get_experiment", "get_monitor", "get_roi_readout"}


def test_no_skill_has_an_engine_grant_and_no_tool_reads_the_workspace_zetta_route():
    for skill in SKILLS.values():
        assert not hasattr(skill, "engine"), skill.name
    assert not hasattr(tools, "workspace_tools")
    assert not hasattr(Lake, "zetta") and not hasattr(Lake, "zetta_openapi")
    source = "\n".join(path.read_text() for path in (ROOT / "src").rglob("*.py"))
    assert "/zetta/v1" not in source.replace("``/workspaces/<customer>/zetta/v1/``", "")


@pytest.mark.parametrize("skill", ORIGINAL_FIVE)
def test_the_original_five_still_reach_what_they_reached_under_lakes_names(skill):
    before = ENGINE_GRANTS_BEFORE[skill]
    if skill != "generate-data":
        before = before | ENGINE_SOLUTION_READS
    for engine_name in before:
        lake_name = ENGINE_TO_LAKE.get(engine_name, engine_name)
        assert lake_name in SKILLS[skill].lake, f"{skill}: {engine_name} -> {lake_name}"
        assert lake_name in CAPABILITIES, lake_name


def test_direct_actions_are_lake_changes_their_skill_grants():
    direct = {name for skill in SKILLS.values() for name in skill.direct}
    assert direct == {"check_pipeline", "start_run", "generate_dataset", "decide",
                      "record_outcome"}
    for skill in SKILLS.values():
        assert skill.direct <= skill.lake, skill.name
        for name in skill.direct:
            assert name in CAPABILITIES, name
            assert CAPABILITIES[name]["action"] != "read", f"{name} is a read, not an action"
            assert name not in never_for_agents(), name
    # A1 approval is a person's: an agent may only propose it.
    assert "approve_decision" in SKILLS["call-api"].lake
    assert "approve_decision" not in SKILLS["call-api"].direct


def test_a_train_agent_reaches_lakes_get_experiment_and_the_holdout_one(tmp_path):
    lake_openapi = openapi_for(["get_experiment", "get_holdout_experiment", "list_projects"])
    fake = FakeLake(lake_openapi)
    derived = {tool.name: tool for tool in tools.all_tools(
        agent_with(["train"], tmp_path), lake_for(fake), lake_openapi)}
    assert {"get_experiment", "get_holdout_experiment", "list_projects"} <= set(derived)

    derived["get_experiment"].call({"id": "exp-1"})
    sent = fake.requests[-1]
    assert sent.url.path == f"/w/{WORKSPACE}/v1/get_experiment"
    derived["list_projects"].call({})
    assert fake.requests[-1].url.path == f"/w/{WORKSPACE}/v1/list_projects"
    assert not any("/zetta/" in request.url.path for request in fake.requests)


def test_a_direct_action_calls_lake_and_any_other_change_is_a_proposal(tmp_path):
    names = ["start_run", "check_pipeline", "start_experiment", "decide", "approve_decision"]
    lake_openapi = openapi_for(names)
    fake = FakeLake(lake_openapi)
    derived = {tool.name: tool for tool in tools.all_tools(
        agent_with(["train", "call-api"], tmp_path), lake_for(fake), lake_openapi)}

    assert not derived["start_run"].proposal and not derived["check_pipeline"].proposal
    assert not derived["decide"].proposal
    assert derived["start_experiment"].proposal and derived["approve_decision"].proposal

    started = derived["start_run"].call({"project": "churn", "graph": "weekly"})
    assert started.ok
    sent = fake.requests[-1]
    assert sent.method == "POST" and sent.url.path == f"/w/{WORKSPACE}/v1/start_run"
    body = json.loads(sent.content)
    assert body["customer_id"] == CUSTOMER and body["workspace_id"] == WORKSPACE

    count = len(fake.requests)
    proposed = derived["approve_decision"].call({"decision_id": "d-1"})
    assert proposed.value["status"] == "proposed" and len(fake.requests) == count


def test_a_direct_action_is_a_proposal_for_a_skill_that_does_not_grant_it_directly(tmp_path):
    lake_openapi = openapi_for(["start_run", "check_pipeline"])
    derived = {tool.name: tool for tool in tools.all_tools(
        agent_with(["pipelines"], tmp_path), lake_for(FakeLake(lake_openapi)), lake_openapi)}
    assert derived["start_run"].proposal and derived["check_pipeline"].proposal


def test_the_work_area_reads_lakes_dataset_id(tmp_path):
    lake_openapi = openapi_for(["get_dataset"])
    fake = FakeLake(lake_openapi)
    agent = agent_with(["search"], tmp_path, work_area={"kind": "dataset", "detail": "accounts"})
    tool = {tool.name: tool for tool in tools.all_tools(
        agent, lake_for(fake), lake_openapi)}["get_dataset"]
    refused = tool.call({"id": "main/payroll"})
    assert not refused.ok and "only on accounts" in refused.error
    assert not fake.requests
    assert tool.call({"id": "main/accounts"}).ok


def test_the_skills_lake_offers_are_the_skills_here():
    assert list(SKILLS) == ["search", "generate-data", "train", "summarize", "call-api",
                            "drives", "transcripts", "repositories", "datasets", "streams",
                            "pipelines", "usage"]


def test_skills_json_is_current():
    spec = importlib.util.spec_from_file_location(
        "write_skills", ROOT / "tools" / "write-skills-json.py")
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    assert (ROOT / "skills.json").read_text() == writer.document(), \
        "skills.json is out of date: run `uv run tools/write-skills-json.py`"
