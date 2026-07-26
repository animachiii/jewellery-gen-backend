"""Small reusable retry helper for the worker's "free" stages (classify, poll,
download/store) — docs/business-rules.md R1: 3 attempts, 2s/8s/30s backoff.

`delays` is a parameter (not a hardcoded sleep) so tests can pass near-zero
delays and stay fast.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

DEFAULT_DELAYS: tuple[float, ...] = (2.0, 8.0, 30.0)


async def retry_free(
    fn: Callable[[], Awaitable[T]],
    *,
    delays: tuple[float, ...] = DEFAULT_DELAYS,
) -> T:
    """Calls `fn()`. On failure, sleeps `delays[i]` and retries, up to
    `len(delays)` retries (i.e. `len(delays) + 1` total attempts). Re-raises
    the last exception if all attempts fail.
    """
    last_exc: Exception | None = None
    attempts = len(delays) + 1
    for attempt in range(attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, re-raised at the end
            last_exc = exc
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
    assert last_exc is not None
    raise last_exc
