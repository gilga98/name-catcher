"""What the app needs from an LLM. Callers depend on these small interfaces, not on a vendor."""

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from django.conf import settings
from django.utils.module_loading import import_string
from pydantic import BaseModel


class AIError(Exception):
    """The AI backend could not answer. Safe to show `str(error)` to the user."""


class AIRateLimited(AIError):
    """No model could take the request, through limits or an outage. It's worth trying again later, unchanged."""


class RespelledName(BaseModel):
    given: str
    sounds_like: str


class NameIdea(BaseModel):
    name: str
    native_script: str
    meaning: str
    origin: str
    respelling: str
    sounds_like: str
    speech_lang: str
    gender: str
    rarity: str
    style: str
    genres: list[str]
    insight: str
    surname_fit: str


class DescribedIdea(NameIdea):
    """`given` is the input echoed back, so answers in a batch can be matched to their names."""

    given: str


@dataclass(frozen=True)
class SuggestionRequest:
    """`brief` must come from `ai.context.brief_for_llm`, which decides what may leave our side."""

    brief: dict
    liked: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    more_like: str = ""
    overused: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    family_suggested: list[str] = field(default_factory=list)
    shown: list[str] = field(default_factory=list)


class NameSuggester(Protocol):
    def suggest(self, request: SuggestionRequest) -> list[NameIdea]: ...

    def describe_many(self, names: list[str], genres: list[str]) -> list[DescribedIdea]: ...

    def respell_many(self, names: list[str]) -> list[RespelledName]: ...




@lru_cache(maxsize=1)
def get_ai():
    return import_string(settings.AI_BACKEND)()
