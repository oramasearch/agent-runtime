"""A turn's credential goes only to Lake at LAKE_URL, never to an address the request names
(agent-runtime#20)."""

from __future__ import annotations

import httpx
import pytest

from agent_runtime import definition, model, runner
from agent_runtime.lake import LoadError

LAKE = "http://lake-prod.lake.svc.cluster.local:80"


@pytest.fixture
def sent(monkeypatch):
    """Every request a model client or Lake client makes; none is answered with data."""
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done"}}],
            "paths": {}})

    real = runner.Lake

    def lake(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(answer)
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "Lake", lake)
    monkeypatch.setattr(runner, "gateway_client", lambda url, token, **kw: model.gateway_client(
        url, token, http_client=httpx.Client(transport=httpx.MockTransport(answer)), **kw))
    return requests


def per_turn_agent(tmp_path):
    return runner.Loaded(definition.from_record(
        {"id": "a-1", "name": "A", "runtime": "python",
         "definition": {"source": "zetta", "files": []}},
        workdir=tmp_path), [], "m")


def test_the_model_is_reached_only_at_lake_url_whatever_the_turn_names(monkeypatch, sent):
    monkeypatch.setenv("LAKE_URL", LAKE)
    client = runner.turn_model(None, {"lake_url": "http://attacker.example",
                                      "lake_token": "lat.task.secret"})
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}])
    assert [(r.url.host, r.url.path) for r in sent] == [
        ("lake-prod.lake.svc.cluster.local", "/openai/v1/chat/completions")]
    assert sent[0].headers["authorization"] == "Bearer lat.task.secret"


@pytest.mark.parametrize("lake_url", [None, "", "  ", "lake-prod/no-scheme", "file:///etc/passwd",
                                      "http://user@lake", "http://lake:notaport"])
def test_without_a_usable_lake_url_no_turn_sends_its_credential(monkeypatch, sent, tmp_path,
                                                                 lake_url):
    if lake_url is None:
        monkeypatch.delenv("LAKE_URL", raising=False)
    else:
        monkeypatch.setenv("LAKE_URL", lake_url)
    monkeypatch.setenv("AGENT_PER_TURN_CREDENTIALS", "1")
    turn = {"message": "hi", "lake_url": LAKE, "lake_token": "lat.task.secret",
            "customer_id": "c-1"}
    with pytest.raises(LoadError, match="LAKE_URL is not set"):
        runner.turn_model(None, turn)
    with pytest.raises(LoadError, match="LAKE_URL is not set"):
        runner.per_turn(per_turn_agent(tmp_path), turn)
    assert sent == []


def test_a_per_turn_lake_client_uses_lake_url(monkeypatch, sent, tmp_path):
    monkeypatch.setenv("LAKE_URL", LAKE + "/")
    monkeypatch.setenv("AGENT_PER_TURN_CREDENTIALS", "1")
    _, lake = runner.per_turn(per_turn_agent(tmp_path), {
        "message": "hi", "lake_url": "http://attacker.example", "lake_token": "lat.task.secret",
        "customer_id": "c-1"})
    assert lake is not None and lake.base_url == LAKE
    lake.close()
    assert sent and all(r.url.host == "lake-prod.lake.svc.cluster.local" for r in sent)
