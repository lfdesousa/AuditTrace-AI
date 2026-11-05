"""The tool-result cache id covers the request context on a real Redis (layer I).

No double at all: a throwaway Redis server (the chart's image, see
``tests/_redis_ephemeral.py``), the real ``ToolResultCache`` over a real
redis-py client, the real ``invoke_tool``, and an observer ``MemoryTool`` as
the oracle. Docker unavailable is a failure, not a skip.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import redis

from audittrace.identity import UserContext
from audittrace.tools import MemoryTool, invoke_tool
from audittrace.tools.cache import (
    ToolResultCache,
    reset_tool_result_cache,
    set_tool_result_cache,
)
from tests._redis_ephemeral import ephemeral_redis


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with ephemeral_redis("audittrace-ck-redis-") as url:
        yield url


@pytest.fixture
def real_cache(redis_url: str) -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.flushdb()
    set_tool_result_cache(ToolResultCache(client, default_ttl_seconds=900))
    yield client
    reset_tool_result_cache()
    client.close()


def _wire_keys(client: redis.Redis) -> int:
    return sum(1 for _ in client.scan_iter(match=f"{ToolResultCache.KEY_PREFIX}*"))


def _user(sub: str) -> UserContext:
    return UserContext(
        user_id=sub,
        username=sub,
        agent_type="curl",
        scopes=("memory:conversational:read-own",),
        is_admin=False,
    )


async def test_two_users_same_session_do_not_share_cached_results_on_real_redis(
    real_cache,
):
    owners: list[str] = []

    async def handler(uc: UserContext, args: dict[str, Any]) -> dict[str, Any]:
        owners.append(uc.user_id)
        return {"matches": [{"owner": uc.user_id}], "total": 1}

    tool = MemoryTool(
        name="recall_attachments",
        description="observer",
        parameters_schema={"type": "object"},
        required_scope="memory:conversational:read-own",
        handler=handler,
    )

    _, alice_hit = await invoke_tool(_user("user-a"), tool, {"n": 5}, "S")
    after_alice = _wire_keys(real_cache)
    bob_result, bob_hit = await invoke_tool(_user("user-b"), tool, {"n": 5}, "S")
    after_bob = _wire_keys(real_cache)

    assert alice_hit is False
    assert bob_hit is False
    assert bob_result["matches"][0]["owner"] == "user-b"
    assert owners == ["user-a", "user-b"]
    assert (after_alice, after_bob) == (1, 2)
