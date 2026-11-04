"""Spot the same name across spellings and Indian scripts: आरव, ಆರವ, Arav and Aaravv match.

Three layers, cheapest first. `phonetic_key` folds spelling and script. `catalog.canonical_key` adds aliases
learned from the LLM and from admins, for names rules can't reach. `find_duplicate` then looks for close
matches, which are only ever offered to a person to confirm. What someone typed is always kept for display.
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Protocol, TypeVar

from indic_transliteration import detect, sanscript
from rapidfuzz import process
from rapidfuzz.distance import JaroWinkler
from unidecode import unidecode

MAYBE_SAME = 0.85  # Close enough to ask the person, who always confirms a merge.
ALMOST_CERTAINLY_SAME = 0.96
MIN_SKELETON = 3  # Shorter consonant outlines match too many different names, like Mira and Mara.
MAX_SPELLING_GAP = 3  # Letters two spellings of one name can differ by in length.
MAX_NAME_LENGTH = 28
MAX_NAME_MARKS = 2
MAX_NAME_WORDS = 3
_NAME_MARKS = "-.'’"
_CAPITALISED_WORD = re.compile(r"[A-Z][a-z]+")

_SPEECH_LANGUAGES = {
    "bengali": "bn-IN", "devanagari": "hi-IN", "gujarati": "gu-IN", "gurmukhi": "pa-IN", "kannada": "kn-IN",
    "malayalam": "ml-IN", "oriya": "or-IN", "tamil": "ta-IN", "telugu": "te-IN",
}  # fmt: skip
_INVISIBLE = re.compile(r"[\s​-‏⁠﻿]+")  # Includes the joiners Indic keyboards insert.
_SOUND_FOLDS = (
    (re.compile(r"x"), "ks"),
    (re.compile(r"ee|ii"), "i"),
    (re.compile(r"oo|uu"), "u"),
    (re.compile(r"ph"), "f"),
    (re.compile(r"w"), "v"),
    (re.compile(r"z"), "j"),
    (re.compile(r"sh"), "s"),
    (re.compile(r"([bcdgjkpt])h"), r"\1"),
    (re.compile(r"(.)\1+"), r"\1"),
)
# Hindi and Kannada drop a final short "a" after one consonant (आरव is Aarav), but not after a cluster (कृष्ण is Krishna).
_IMPLIED_FINAL_VOWEL = re.compile(r"([aeiouAEIOU](?:[kgcjTDtdpb]h|[sS]h|[^aeiouAEIOU]))a$")
# Tamil writes k/g, t/d, p/b and s/j with one letter each, so its names can't be told apart by those sounds.
_VOICING = str.maketrans("gdbj", "ktps")
_VOWELS = re.compile(r"[aeiouy]")


class HasKey(Protocol):
    key: str


C = TypeVar("C", bound=HasKey)


def clean(text: str) -> str:
    return _INVISIBLE.sub("", unicodedata.normalize("NFC", text))


def speech_language(text: str) -> str:
    """The language a name's own script tells us, available instantly and with no LLM."""
    return _SPEECH_LANGUAGES.get(detect.detect(clean(text)), "")


def _romanize(text: str) -> str:
    scheme = detect.detect(text)
    if scheme not in _SPEECH_LANGUAGES:
        return unidecode(text)
    return _IMPLIED_FINAL_VOWEL.sub(r"\1", sanscript.transliterate(text, scheme, sanscript.ITRANS))


def phonetic_key(text: str) -> str:
    key = re.sub(r"[^a-z]", "", _romanize(clean(text)).lower())
    for pattern, replacement in _SOUND_FOLDS:
        key = pattern.sub(replacement, key)
    return key


def _skeleton(key: str) -> str:
    return re.sub(r"(.)\1+", r"\1", _VOWELS.sub("", key.translate(_VOICING)))


def closest(key: str, candidates: Iterable[C], cutoff: float) -> C | None:
    pool = list(candidates)
    match = process.extractOne(
        key,
        {index: candidate.key for index, candidate in enumerate(pool)},
        scorer=JaroWinkler.normalized_similarity,
        score_cutoff=cutoff,
    )
    return pool[match[2]] if match else None


def _same_outline(key: str, candidates: Iterable[C]) -> C | None:
    """Catches Mohammed and Muhammad, or கிருஷ்ணா and Krishna, where spelling distance alone falls short."""
    outline = _skeleton(key)
    if len(outline) < MIN_SKELETON:
        return None
    return next((candidate for candidate in candidates if _skeleton(candidate.key) == outline), None)


def find_duplicate(text: str, candidates: Iterable[C], key: str = "") -> C | None:
    key = key or phonetic_key(text)
    pool = list(candidates)
    return closest(key, pool, MAYBE_SAME) or _same_outline(key, pool)


def same_name(text: str, key: str) -> bool:
    """Whether this can be another spelling of `key`. Similarity favours a shared start, so length has to count too."""
    other = phonetic_key(text)
    if abs(len(other) - len(key)) > MAX_SPELLING_GAP:
        return False
    outline = _skeleton(other)
    similar = JaroWinkler.normalized_similarity(other, key) >= MAYBE_SAME
    return similar or (len(outline) >= MIN_SKELETON and outline == _skeleton(key))


def looks_like_name(text: str) -> bool:
    """A name, not a sentence with its spaces squeezed out. What people type reaches the LLM, the voice and others."""
    cleaned = clean(text)
    return (
        len(cleaned) <= MAX_NAME_LENGTH
        and sum(character in _NAME_MARKS for character in cleaned) <= MAX_NAME_MARKS
        and len(_CAPITALISED_WORD.findall(cleaned)) <= MAX_NAME_WORDS
        and all(character in _NAME_MARKS or unicodedata.category(character)[0] in "LM" for character in cleaned)
    )
