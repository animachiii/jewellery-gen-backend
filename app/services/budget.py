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


async def check_budget(redis: Redis) -> bool:
    """Read-only check. Returns True if today's billable count is under the cap."""
    raw = await redis.get(_spend_key(_today()))
    count = int(raw) if raw is not None else 0
    return count < settings.daily_generation_cap


async def increment_spend(redis: Redis) -> None:
    key = _spend_key(_today())
    await redis.incr(key)
    await redis.expire(key, _SPEND_TTL_SECONDS)
