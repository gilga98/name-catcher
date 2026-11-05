"""Models take turns, so one model's quota doesn't run out while the others sit unused, and one that fails sits out.

This lives in the shared cache so every worker agrees, and the web app can see when AI is resting without calling it.
If the cache is down, turns stop rotating and nothing sits out, which costs a few wasted calls and breaks nothing.
"""

import math
import time

from django.core.cache import cache

_TURN, _RESTING = "ai-model-turn", "ai-resting"


def _cooling_key(model: str) -> str:
    return f"ai-model-cooling-{model}"


def _cooling(models: list[str]) -> dict:
    return cache.get_many([_cooling_key(model) for model in models])


def in_turn(models: list[str]) -> list[str]:
    """Each call starts one model further along. Those sitting out aren't offered, so they cost no time or quota."""
    cache.add(_TURN, 0, timeout=None)
    try:
        turn = cache.incr(_TURN) or 0
    except ValueError:  # The key went missing between those two lines.
        turn = 0
    start = turn % len(models)
    cooling = _cooling(models)
    return [model for model in [*models[start:], *models[:start]] if _cooling_key(model) not in cooling]


def cool(model: str, seconds: int) -> None:
    cache.set(_cooling_key(model), time.time() + seconds, seconds)


def rest_if_all_cooling(models: list[str]) -> None:
    """Until the first model returns, pages can skip AI at once rather than make someone wait to hear it's busy."""
    cooling = _cooling(models)
    if len(cooling) == len(models):
        seconds = math.ceil(min(cooling.values()) - time.time())
        if seconds > 0:
            cache.set(_RESTING, 1, seconds)


def resting() -> bool:
    return bool(cache.get(_RESTING))
