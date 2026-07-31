"""Daily generation cap (docs/business-rules.md R5).

`spend:{YYYY-MM-DD}` counts billable submits for the current UTC day. Mock and
deduplicated jobs are not billable and never increment it (R6).
"""

from datetime import UTC, datetime

from redis.asyncio import Redis

from app.config import settings

_SPEND_TTL_SECONDS = 48 * 60 * 60


def _spend_key(date: str) -> str:
    return f"spend:{date}"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def today_spend(redis: Redis) -> int:
    """Read-only: today's billable submit count. Phase 8 -- exposed via
    GET /health/deep so an operator can see "180/200" before the cap is hit,
    rather than discovering it only when clients start getting 429s."""
    raw = await redis.get(_spend_key(_today()))
    return int(raw) if raw is not None else 0


async def check_budget(redis: Redis) -> bool:
    """Read-only check. Returns True if today's billable count is under the cap."""
    count = await today_spend(redis)
    return count < settings.daily_generation_cap


async def increment_spend(redis: Redis) -> None:
    key = _spend_key(_today())
    await redis.incr(key)
    await redis.expire(key, _SPEND_TTL_SECONDS)
