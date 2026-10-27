"""Per-process TTL memo for slow-changing reads on hot paths. Each Cloud Run
instance keeps its own copy, so a change is visible everywhere within `ttl`."""

import time

_store: dict = {}


async def cached(key, ttl: float, fetch):
    now = time.monotonic()
    hit = _store.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = await fetch()
    _store[key] = (now, value)
    return value


def clear() -> None:
    _store.clear()
