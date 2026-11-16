# SPDX-License-Identifier: MIT

"""The model, reached through lake's model gateway (lake#248, lake `docs/api/model-gateway.md`).

Every model call this agent makes for a turn or run goes to lake at `<lake_url>/openai/v1`, with the
run's own lake credential as the API key: a turn's `lake_token`, or a launch's `LAKE_TOKEN`. Lake
sends it to the model provider with Orama's own key, counts its tokens as the `model_tokens` of the
workspace the run belongs to, and refuses it once the account's monthly limit is used up. The agent
holds no provider key and reports no usage of its own.
"""

from __future__ import annotations

import httpx
from openai import OpenAI


def gateway_url(lake_url: str) -> str:
    return f"{lake_url.rstrip('/')}/openai/v1"


def gateway_client(lake_url: str, lake_token: str,
                   http_client: httpx.Client | None = None) -> OpenAI:
    """An OpenAI client whose every call goes through lake for this run."""
    if not lake_url or not lake_token:
        raise ValueError("a model call needs lake's URL and the run's lake credential")
    return OpenAI(base_url=gateway_url(lake_url), api_key=lake_token, http_client=http_client)
