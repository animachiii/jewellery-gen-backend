from collections.abc import AsyncGenerator

import pytest_asyncio
from redis.asyncio import Redis

TEST_REDIS_URL = "redis://localhost:6379/15"


@pytest_asyncio.fixture
async def redis() -> AsyncGenerator[Redis, None]:
    client: Redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()
