"""Dataset work areas reject unprovable resources before Lake sees a call."""

from __future__ import annotations

import httpx
import pytest

from agent_runtime import definition, tools
from agent_runtime.lake import Lake
from test_skills import CAPABILITIES, SKILLS, openapi_for


def setup_tools(tmp_path, value=None, detail="allowed_public/accounts", names=None, error=None):
    agent = definition.from_record({
        "id": "a", "name": "Confined", "runtime": "python",
        "skills": ["search", "datasets", "train", "call-api"],
        "work_area": {"kind": "dataset", "detail": detail},
        "definition": {"source": "zetta", "files": []},
    }, workdir=tmp_path)
    requests = []

    def respond(request):
        requests.append(request)
        if error:
            return httpx.Response(400, json={"ok": False, "error": error})
        return httpx.Response(200, json={"ok": True, "value": value})

    lake = Lake("http://lake", "test-token", "customer", workspace_id="workspace",
                transport=httpx.MockTransport(respond))
    api = openapi_for(names or ["get_dataset", "list_datasets", "search_workspace_index",
                               "search_index", "get_index", "list_indexes", "search"])
    derived = {tool.name: tool for tool in tools.lake_tools(lake, api, agent)}
    return agent, lake, requests, derived


@pytest.mark.parametrize("arguments", [
    {"id": "restricted/accounts"}, {"id": "restricted.accounts"},
    {"id": "accounts"}, {"id": "Allowed_public/accounts"},
    {"id": "allowed_public%2faccounts"}, {"id": "allowed_public/../accounts"},
    {"id": "allowed_public/accounts/"}, {"id": " allowed_public/accounts"},
    {"id": "allowed_public/accounts", "database": "restricted"},
    {"id": "allowed_public/accounts", "dataset": "restricted/accounts"},
    {"dataset": "allowed_public/accounts"}, {"id": ["allowed_public/accounts"]},
    {"id": ""}, {}, [], None,
])
def test_dataset_selector_cannot_change_database_or_hide_a_second_selector(tmp_path, arguments):
    _, _, requests, derived = setup_tools(tmp_path)
    assert not derived["get_dataset"].call(arguments).ok
    assert not requests


@pytest.mark.parametrize("selector", ["allowed_public/accounts", "allowed_public.accounts"])
def test_qualified_dataset_aliases_work(tmp_path, selector):
    row = {"database": "allowed_public", "name": "accounts", "columns": ["public"]}
    _, _, requests, derived = setup_tools(tmp_path, row)
    reply = derived["get_dataset"].call({"id": selector})
    assert reply.ok and reply.value == row
    assert requests[0].url.params["id"] == "allowed_public/accounts"


def test_bare_dataset_is_only_the_lake_default_database(tmp_path):
    row = {"database": "datasets", "name": "accounts"}
    _, _, requests, derived = setup_tools(tmp_path, row, detail="accounts")
    assert not derived["get_dataset"].call({"id": "restricted/accounts"}).ok
    assert not requests
    assert derived["get_dataset"].call({"id": "accounts"}).ok
    assert requests[0].url.params["id"] == "datasets/accounts"


@pytest.mark.parametrize("name, arguments", [
    ("search_workspace_index", {"target": "outside-index", "query": "private"}),
    ("search_workspace_index", {"target": "accounts", "query": "private"}),
    ("search_workspace_index", {"target": "outside-index", "query": "private",
                                "dataset": "allowed_public/accounts"}),
    ("search_index", {"index_id": "outside-index", "query": "private"}),
    ("get_index", {"index_id": "outside-index"}),
    ("list_indexes", {}), ("search", {"query": "private"}),
])
def test_unverifiable_index_sources_and_global_search_make_no_outbound_call(tmp_path, name, arguments):
    _, _, requests, derived = setup_tools(tmp_path)
    reply = derived[name].call(arguments)
    assert not reply.ok
    assert not requests


def test_list_datasets_drops_other_databases_ambiguous_rows_and_unscoped_metadata(tmp_path):
    allowed = {"id": "allowed_public/accounts", "database": "allowed_public",
               "name": "accounts", "columns": ["public"]}
    value = {"datasets": [allowed,
                          {"database": "restricted", "name": "accounts", "columns": ["secret"]},
                          {"name": "accounts", "columns": ["secret"]},
                          {**allowed, "id": "restricted/accounts"},
                          "restricted/accounts"], "outside_summary": "secret"}
    _, _, _, derived = setup_tools(tmp_path, value)
    reply = derived["list_datasets"].call({})
    assert reply.ok and reply.value == {"datasets": [allowed]}
    assert "secret" not in reply.observation()


@pytest.mark.parametrize("value", [None, {}, {"datasets": "secret"},
                                  [{"database": "restricted", "name": "accounts"}]])
def test_unknown_list_response_fails_closed(tmp_path, value):
    _, _, _, derived = setup_tools(tmp_path, value)
    reply = derived["list_datasets"].call({})
    assert not reply.ok and reply.value is None


def test_multiple_dataset_grants_preserve_database_identity(tmp_path):
    rows = [{"database": "allowed_public", "name": "accounts"},
            {"database": "reports", "name": "totals"},
            {"database": "restricted", "name": "totals"}]
    _, _, _, derived = setup_tools(tmp_path, {"datasets": rows},
                                  detail="allowed_public/accounts, reports.totals")
    assert derived["list_datasets"].call({}).value == {"datasets": rows[:2]}


@pytest.mark.parametrize("name, arguments", [("get_dataset", {"id": "allowed_public/accounts"}),
                                           ("list_datasets", {})])
def test_server_errors_cannot_echo_outside_dataset_details(tmp_path, name, arguments):
    _, _, _, derived = setup_tools(tmp_path, error={"code": "invalid_input",
                                                  "message": "restricted/accounts: secret"})
    reply = derived[name].call(arguments)
    assert not reply.ok and reply.value is None
    assert "secret" not in reply.observation() and "restricted" not in reply.observation()


@pytest.mark.parametrize("row", [
    {"database": "restricted", "name": "accounts", "columns": ["secret"]},
    {"name": "accounts", "columns": ["secret"]},
    {"id": "restricted/accounts", "database": "allowed_public", "name": "accounts"},
])
def test_dataset_response_identity_is_checked_before_returning_it(tmp_path, row):
    _, _, _, derived = setup_tools(tmp_path, row)
    reply = derived["get_dataset"].call({"id": "allowed_public/accounts"})
    assert not reply.ok and reply.value is None


@pytest.mark.parametrize("detail", ["", " ", "allowed_public/accounts,", "../accounts",
                                  "allowed_public/accounts,restricted//accounts"])
def test_invalid_work_area_never_means_unrestricted(tmp_path, detail):
    _, _, requests, derived = setup_tools(tmp_path, detail=detail)
    for name in ["get_dataset", "list_datasets", "search_workspace_index"]:
        assert not derived[name].call({"id": "restricted/accounts", "target": "outside"}).ok
    assert not requests


def test_every_other_granted_read_or_direct_action_fails_closed(tmp_path):
    agent, lake, requests, _ = setup_tools(tmp_path)
    agent.skills = list(SKILLS)
    api = openapi_for(list(CAPABILITIES))
    for tool in tools.lake_tools(lake, api, agent):
        if tool.proposal or tool.name in {"get_dataset", "list_datasets"}:
            continue
        assert not tool.call({"dataset": "allowed_public/accounts"}).ok, tool.name
    assert not requests


def test_exact_proposals_remain_available_without_outbound_calls(tmp_path):
    _, _, requests, derived = setup_tools(tmp_path, names=["create_index"])
    arguments = {"dataset": "allowed_public/accounts"}
    reply = derived["create_index"].call(arguments)
    assert derived["create_index"].proposal
    assert reply.ok and reply.value["status"] == "proposed"
    assert reply.value["input"] == arguments
    assert not requests


def test_dataset_area_has_no_pipeline_mcp_or_definition_tool_escape(tmp_path):
    agent, lake, requests, _ = setup_tools(tmp_path)
    agent.pipelines = [{"project": "private", "graph": "private"}]
    agent.skills.append("mcp:outside")
    package = agent.directory / "agent"
    package.mkdir()
    (package / "tools.py").write_text('raise RuntimeError("must not import")\n')
    assert tools.pipeline_tools(agent, lake) == []
    assert tools.definition_tools(agent) == []
    from agent_runtime.mcp import mcp_tools
    assert mcp_tools(agent, lake) == []
    assert not requests
    derived = tools.all_tools(agent, lake, openapi_for(["get_dataset"]))
    assert [tool.name for tool in derived] == ["get_dataset"]
    assert not requests
