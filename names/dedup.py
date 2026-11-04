"""Spot the same name across spellings and Indian scripts: आरव, ಆರವ, Arav and Aaravv match."""

import re
from collections.abc import Iterable
from typing import Protocol, TypeVar

from indic_transliteration import detect, sanscript
from rapidfuzz import process
from rapidfuzz.distance import JaroWinkler
from unidecode import unidecode

MAYBE_SAME = 0.85  # Close enough to ask the person, who always confirms a merge.
ALMOST_CERTAINLY_SAME = 0.96

_INDIC_SCRIPTS = {
    "bengali", "devanagari", "gujarati", "gurmukhi", "kannada", "malayalam", "oriya", "tamil", "telugu",
}  # fmt: skip
_SOUND_FOLDS = (
    (re.compile(r"ee|ii"), "i"),
    (re.compile(r"oo|uu"), "u"),
    (re.compile(r"ph"), "f"),
    (re.compile(r"w"), "v"),
    (re.compile(r"z"), "j"),
    (re.compile(r"sh"), "s"),
    (re.compile(r"([bcdgkpt])h"), r"\1"),
    (re.compile(r"(.)\1+"), r"\1"),
)
_IMPLIED_FINAL_VOWEL = re.compile(r"(?<=[^aA])a$")


class HasKey(Protocol):
    key: str


C = TypeVar("C", bound=HasKey)


def _romanize(text: str) -> str:
    scheme = detect.detect(text)
    if scheme not in _INDIC_SCRIPTS:
        return unidecode(text)
    return _IMPLIED_FINAL_VOWEL.sub("", sanscript.transliterate(text, scheme, sanscript.ITRANS))


def phonetic_key(text: str) -> str:
    key = re.sub(r"[^a-z]", "", _romanize(text).lower())
    for pattern, replacement in _SOUND_FOLDS:
        key = pattern.sub(replacement, key)
    return key


def closest(key: str, candidates: Iterable[C], cutoff: float) -> C | None:
    pool = list(candidates)
    match = process.extractOne(
        key,
        {index: candidate.key for index, candidate in enumerate(pool)},
        scorer=JaroWinkler.normalized_similarity,
        score_cutoff=cutoff,
    )
    return pool[match[2]] if match else None


def find_duplicate(text: str, candidates: Iterable[C]) -> C | None:
    return closest(phonetic_key(text), candidates, MAYBE_SAME)
