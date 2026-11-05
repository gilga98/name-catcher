"""The shared name pool: what we can answer without an LLM, and how people and admins grow it."""

import csv
import hashlib
import io
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache

from django.conf import settings
from django.db.models import Count, F, Max, Q
from django.utils import timezone
from rapidfuzz.distance import JaroWinkler

from . import jobs
from .ai.base import NameIdea
from .ai.context import scrub
from .ai.rotation import resting
from .dedup import looks_like_name, phonetic_key, same_name
from .models import CatalogName, Genre, Name, NameAlias

VALID_NAME = re.compile(r"^[^\d<>/:@]{1,40}$")
COLUMNS = [
    "name", "meaning", "origin", "gender", "genres", "native", "respelling", "speech_lang", "rarity", "style",
    "insight", "variants", "sounds_like",
]  # fmt: skip
# The names an LLM reaches for first, whatever the brief. Popularity from our own boards is added to these.
ALWAYS_OVERUSED = (
    "Aarav", "Vivaan", "Vihaan", "Aditya", "Arjun", "Reyansh", "Ayaan", "Krishna", "Ishaan", "Kiaan",
    "Aadhya", "Ananya", "Diya", "Saanvi", "Myra", "Kiara", "Anaya", "Pari", "Aarohi", "Navya",
)  # fmt: skip
_VOWELS = "aeiou"
_MEANING_WORDS, _INSIGHT_WORDS = 12, 20
RETRY_FAILED_AFTER = timedelta(hours=1)
DETAILS_PATIENCE = timedelta(seconds=60)
SAME_SPELLING = 0.7  # Below this, the LLM has changed the name rather than its spelling, so it isn't merged.
_WORD_BREAK = re.compile(r"[\s,.;:!?()\"'।॥-]+")
_MARKUP = re.compile(r"[*_`#]+")
_SOUND_SPELLING = re.compile(r"[a-z]{1,30}( [a-z]{1,30}){0,2}")
# Lets a parent's own words find catalogue genres in the browser, without a call to the LLM.
_GENRE_WORDS = {
    "Nature": "nature river rivers tree trees forest flower flowers lotus earth mountain mountains rain ocean sea leaf",
    "Light": "light bright dawn sun sunrise radiant radiance glow shine shining prakash jyoti surya kiran",
    "Strength": "strength strong brave courage courageous bold fearless warrior power resilient",
    "Wisdom": "wisdom wise knowledge learning intelligent scholar clever gyan vidya",
    "Peace": "peace peaceful calm gentle quiet serene shanti kindness kind",
    "Joy": "joy joyful happy happiness laughter delight cheerful anand",
    "Devotion": "devotion devoted faith god goddess divine prayer spiritual bhakti blessing blessed gift",
    "Love": "love loving beloved affection tender prem heart",
    "Mythology": "mythology myth epic ramayana mahabharata legend purana",
    "Literature": "literature poetry poem poet poetic story book books kavya",
    "Vedic": "vedic veda vedas sanskrit sacred",
    "Celestial": "star stars moon sky cosmos planet galaxy celestial heaven tara chandra",
}


def _approved():
    return CatalogName.objects.filter(status=CatalogName.Status.APPROVED)


def version(brief: dict) -> str:
    """Changes whenever the shard's contents can, so browsers can cache a shard URL for a long time."""
    stats = _approved().aggregate(count=Count("pk"), latest=Max("updated_at"))
    return f"{stats['count']}-{stats['latest'].timestamp() if stats['latest'] else 0:.0f}-{brief_stamp(brief)}"


def brief_stamp(brief: dict) -> str:
    return hashlib.sha256(json.dumps(brief, sort_keys=True).encode()).hexdigest()[:8]


def _for_brief(brief: dict):
    names = _approved()
    gender = brief.get("gender") or "any"
    if gender != "any":
        names = names.filter(gender__in=[gender, "any", "neutral"])
    if brief.get("origins"):
        names = names.filter(origin__in=brief["origins"])
    style = brief.get("style") or "any"
    if style != "any":
        names = names.filter(style__in=[style, "any"])
    return names


def shard(brief: dict) -> list[dict]:
    """Just enough for a browser to rank names. Meanings are only sent for the names it picks."""
    names = _for_brief(brief).prefetch_related("genres")[: settings.CATALOG_SHARD_LIMIT]
    return [
        {"id": n.pk, "t": n.text, "k": n.key, "r": n.rarity, "g": [genre.name for genre in n.genres.all()]}
        for n in names
    ]


def pick(ids: list[int]) -> list[CatalogName]:
    """In the order given: that order is the browser's ranking."""
    found = _approved().prefetch_related("genres").in_bulk(ids)
    return [found[pk] for pk in ids if pk in found]


def record_picks(entries: Iterable[CatalogName]) -> None:
    CatalogName.objects.filter(pk__in=[e.pk for e in entries]).update(picks=F("picks") + 1)


def canonical_key(text: str) -> str:
    """The phonetic key, then whichever entry we've learned that spelling belongs to."""
    key = phonetic_key(text)[:60]
    alias = NameAlias.objects.filter(key=key).select_related("entry").first()
    return alias.entry.key if alias else key


def lookup(key: str) -> CatalogName | None:
    """A known answer for this name. Asked about alone, the LLM saw only the name, so that's no worse than asking
    again. Text from a parent's brief or a contributor is shaped by what they wrote, so it waits for a person."""
    trusted = Q(status=CatalogName.Status.APPROVED) | Q(
        status=CatalogName.Status.PENDING, enrich_state=CatalogName.Enrich.DONE
    )
    return CatalogName.objects.filter(trusted, key=key).exclude(meaning="").first()


def request_enrichment(text: str, key: str) -> bool:
    """Queues this name for the next LLM batch, unless it's already waiting. False means it won't get one."""
    waiting = {"text": text[:60], "source": CatalogName.Source.LLM, "enrich_state": CatalogName.Enrich.PENDING}
    entry, _ = CatalogName.objects.get_or_create(key=key, defaults=waiting)
    if entry.status == CatalogName.Status.REJECTED:
        return False
    if entry.enrich_state == CatalogName.Enrich.FAILED and timezone.now() - entry.updated_at < RETRY_FAILED_AFTER:
        return False
    if entry.enrich_state in (CatalogName.Enrich.NONE, CatalogName.Enrich.FAILED):
        entry.enrich_state, entry.enrich_attempts = CatalogName.Enrich.PENDING, 0
        entry.save(update_fields=["enrich_state", "enrich_attempts", "updated_at"])
    jobs.kick(jobs.LLM)
    return True


def being_described(key: str) -> bool:
    waiting = [CatalogName.Enrich.PENDING, CatalogName.Enrich.RUNNING]
    return CatalogName.objects.filter(key=key, enrich_state__in=waiting).exists()


def worth_waiting_for(key: str) -> bool:
    """Whether a card should keep saying details are coming. They can still arrive later, for the next look at it."""
    if resting():
        return False
    waiting = [CatalogName.Enrich.PENDING, CatalogName.Enrich.RUNNING]
    recent = timezone.now() - DETAILS_PATIENCE
    return CatalogName.objects.filter(key=key, enrich_state__in=waiting, updated_at__gte=recent).exists()


def queue_blank_entries(entries) -> None:
    """Admins can upload bare names and let the batches fill them in."""
    entries.filter(meaning="", enrich_state=CatalogName.Enrich.NONE).update(enrich_state=CatalogName.Enrich.PENDING)
    jobs.kick(jobs.LLM)


def wait_for_batch() -> None:
    """A brief pause lets names arriving at about the same time share one LLM call."""
    if settings.AI_ENRICH_BATCH_WINDOW_SECONDS:
        time.sleep(settings.AI_ENRICH_BATCH_WINDOW_SECONDS)


def _merge_spelling(entry: CatalogName, canonical: str) -> CatalogName:
    """The LLM says this is another spelling of `canonical`. Remember that, so this one never costs a call again."""
    old_key = entry.key
    target = CatalogName.objects.filter(key=canonical).exclude(pk=entry.pk).first()
    if target is None:
        entry.key = canonical
        entry.save(update_fields=["key"])
        target = entry
    elif not (entry.source == CatalogName.Source.USER or entry.picks):
        entry.delete()
    NameAlias.objects.update_or_create(key=old_key, defaults={"entry": target})
    Name.objects.filter(key=old_key).update(key=target.key)
    return target


def _store_answer(entry: CatalogName, idea: NameIdea) -> None:
    # Text from a contributor or a parent's brief hasn't been reviewed, so it mustn't be what other families are shown.
    unreviewed = entry.status == CatalogName.Status.PENDING and entry.source != CatalogName.Source.ADMIN
    for attribute, value in (
        ("meaning", idea.meaning[:300]),
        ("insight", idea.insight[:200]),
        ("origin", idea.origin[:80]),
        ("native", idea.native_script[:60]),
        ("respelling", idea.respelling[:80]),
        ("sounds_like", idea.sounds_like),
        ("speech_lang", idea.speech_lang[:20]),
    ):
        if unreviewed or not getattr(entry, attribute):
            setattr(entry, attribute, value)


def _store_labels(entry: CatalogName, idea: NameIdea) -> None:
    """An admin's choices on a live entry stand. Everything else takes the LLM's reading."""
    if entry.status == CatalogName.Status.PENDING or entry.source == CatalogName.Source.LLM:
        entry.gender = _choice(idea.gender, CatalogName.Gender, "any")
        entry.rarity = _choice(idea.rarity, CatalogName.Rarity, "uncommon")
        entry.style = _choice(idea.style, CatalogName.Style, "any")


def apply_idea(entry: CatalogName, idea: NameIdea) -> CatalogName:
    """Stores the LLM's answer on the shared entry. Returns the entry that holds it, which may be another spelling."""
    canonical = phonetic_key(idea.name)[:60]
    same_name = JaroWinkler.normalized_similarity(canonical, entry.key) >= SAME_SPELLING
    if canonical and canonical != entry.key and same_name:
        entry = _merge_spelling(entry, canonical)
    _store_answer(entry, idea)
    _store_labels(entry, idea)
    if not entry.text.isascii() and idea.name.isascii():
        entry.text = idea.name[:60]
    entry.enrich_state = CatalogName.Enrich.DONE
    entry.save()
    entry.genres.add(*Genre.objects.filter(name__in=idea.genres))
    return entry


def overused_names() -> list[str]:
    popular = _approved().filter(picks__gt=0).order_by("-picks").values_list("text", flat=True)[:30]
    return sorted({*ALWAYS_OVERUSED, *popular})


@lru_cache(maxsize=1)
def _genre_keys() -> dict[str, str]:
    return {phonetic_key(word): genre for genre, vocabulary in _GENRE_WORDS.items() for word in vocabulary.split()}


def themes_from_text(text: str) -> list[str]:
    """Works in any script: ಸೂರ್ಯ and सूर्य reach the same genre as "surya", because they share a key."""
    keys = {phonetic_key(word) for word in _WORD_BREAK.split(text)}
    return sorted({genre for key, genre in _genre_keys().items() if key in keys})


def tidy(text: str, max_words: int) -> str:
    """Models ignore length limits often enough that the limit has to be enforced here as well."""
    words = _MARKUP.sub("", text).split()
    return " ".join(words[:max_words]).rstrip(",;:")


def _without_contact_details(text: str) -> str:
    """No true fact about a name holds a link or a phone number, so one in an answer means the LLM was misled."""
    return text if scrub(text) == text else ""


def checked_sounds_like(spelling: str, name: str) -> str:
    """Phones read this aloud, so it has to be plain letters that could really be this name, and not a message."""
    spelling = spelling.strip().lower()
    return spelling if _SOUND_SPELLING.fullmatch(spelling) and same_name(spelling, phonetic_key(name)) else ""


def tidy_idea(idea: NameIdea) -> NameIdea:
    return idea.model_copy(
        update={
            "sounds_like": checked_sounds_like(idea.sounds_like, idea.name),
            "meaning": _without_contact_details(tidy(idea.meaning, _MEANING_WORDS)),
            "insight": _without_contact_details(tidy(idea.insight, _INSIGHT_WORDS)[:200]),
            "surname_fit": _without_contact_details(tidy(idea.surname_fit, _INSIGHT_WORDS)[:200]),
        }
    )


def tags_for(origin: str, rarity: str, style: str, genres: Iterable[str]) -> list[str]:
    """The short labels on a card, most useful first."""
    tags = [origin, rarity.capitalize(), "" if style == "any" else style.capitalize(), *genres]
    return [tag for tag in tags if tag][:5]


def ideas_for(brief: dict, exclude: set[str], limit: int = 8) -> list[CatalogName]:
    """For a family member tapping through ideas. Names that match the brief's themes come first."""
    themes = set(brief.get("themes", []))
    pool = _for_brief(brief).exclude(key__in=exclude).prefetch_related("genres").order_by("picks")[:200]
    return sorted(pool, key=lambda n: -len(themes & {genre.name for genre in n.genres.all()}))[:limit]


def close_matches(brief: dict, exclude: set[str], limit: int = 8) -> list[CatalogName]:
    """For when AI can't help. Style and origin are preferences a family can flex on, but gender stays as asked."""
    wider = {key: value for key, value in brief.items() if key not in ("style", "origins")}
    return ideas_for(brief, exclude, limit) or ideas_for(wider, exclude, limit)


def genre_names() -> list[str]:
    return list(Genre.objects.filter(active=True).values_list("name", flat=True))


def _choice(value: str, choices, default: str) -> str:
    return value if value in choices.values else default


def remember_ideas(ideas: Iterable[NameIdea]) -> None:
    """Keep every paid answer. It waits for a human, then serves other families for free."""
    known_genres = {genre.name: genre for genre in Genre.objects.all()}
    for idea in ideas:
        key = phonetic_key(idea.name)[:60]
        if not key:
            continue
        entry, created = CatalogName.objects.get_or_create(
            key=key,
            defaults={
                "text": idea.name[:60],
                "native": idea.native_script[:60],
                "meaning": idea.meaning[:300],
                "insight": idea.insight[:200],
                "origin": idea.origin[:80],
                "respelling": idea.respelling[:80],
                "sounds_like": idea.sounds_like,
                "speech_lang": idea.speech_lang[:20],
                "gender": _choice(idea.gender, CatalogName.Gender, "any"),
                "rarity": _choice(idea.rarity, CatalogName.Rarity, "uncommon"),
                "style": _choice(idea.style, CatalogName.Style, "any"),
                "source": CatalogName.Source.LLM,
            },
        )
        if created:
            entry.genres.set(known_genres[name] for name in idea.genres if name in known_genres)


def surname_fit(text: str, surname: str) -> str:
    """A plain observation about how the two names sound together. Needs no LLM, and works in any script."""
    first, last = phonetic_key(text), phonetic_key(surname)
    if not first or not last:
        return ""
    if first[-1] in _VOWELS and last[0] in _VOWELS:
        return f"The vowels run together into {surname}, so say it with a small pause."
    if first[-1] == last[0]:
        return f"The last sound of the name blends into {surname}."
    if first[0] == last[0]:
        return f"Alliterates with {surname}."
    return f"Clear break before {surname}."


@dataclass
class ImportResult:
    created: int = 0
    merged: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImportOptions:
    """Who the rows came from decides whether they go live, and whether they may touch existing entries."""

    source: str
    status: str
    genres: tuple[Genre, ...] = ()
    origin: str = ""
    gender: str = "any"
    submitted_by: object = None
    row_limit: int = 500

    @property
    def trusted(self) -> bool:
        return self.source == CatalogName.Source.ADMIN


def _rows(text: str) -> Iterable[dict]:
    first_line = text.lstrip().partition("\n")[0].lower()
    has_header = first_line.startswith("name,") or first_line.strip() == "name"
    reader = csv.DictReader(io.StringIO(text), fieldnames=None if has_header else COLUMNS, skipinitialspace=True)
    for row in reader:
        yield {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if isinstance(v, str)}


def _fields_from(row: dict, options: ImportOptions, key: str) -> dict:
    return {
        "key": key,
        "text": row["name"][:60],
        "meaning": row.get("meaning", "")[:300],
        "insight": row.get("insight", "")[:200],
        "origin": (row.get("origin") or options.origin)[:80],
        "native": row.get("native", "")[:60],
        "respelling": row.get("respelling", "")[:80],
        "sounds_like": checked_sounds_like(row.get("sounds_like", ""), row["name"]),
        "speech_lang": row.get("speech_lang", "")[:20],
        "gender": _choice(row.get("gender", ""), CatalogName.Gender, options.gender),
        "rarity": _choice(row.get("rarity", ""), CatalogName.Rarity, "uncommon"),
        "style": _choice(row.get("style", ""), CatalogName.Style, "any"),
        "status": options.status,
        "source": options.source,
        "submitted_by": options.submitted_by,
    }


def _row_genres(row: dict, options: ImportOptions, known: dict[str, Genre]) -> list[Genre]:
    named = [known[part.strip().lower()] for part in row.get("genres", "").split("|") if part.strip().lower() in known]
    return [*named, *options.genres]


def _merge(entry: CatalogName, fields: dict, genres: list[Genre], options: ImportOptions, result: ImportResult) -> None:
    if not options.trusted:
        # Strangers can't edit what's already live, but agreement between them is a useful signal to moderators.
        user = options.submitted_by
        if entry.submitted_by_id != user.pk and not entry.supporters.filter(pk=user.pk).exists():
            entry.supporters.add(user)
            CatalogName.objects.filter(pk=entry.pk).update(submissions=F("submissions") + 1)
        result.duplicates += 1
        return
    for name in ("meaning", "insight", "origin", "native", "respelling", "sounds_like", "speech_lang"):
        if fields[name] and not getattr(entry, name):
            setattr(entry, name, fields[name])
    entry.status = options.status
    entry.save()
    entry.genres.add(*genres)
    result.merged += 1


def _add_variants(entry: CatalogName, row: dict, options: ImportOptions) -> None:
    if not options.trusted:
        return
    for variant in row.get("variants", "").split("|"):
        key = phonetic_key(variant)[:60]
        if key and key != entry.key and not CatalogName.objects.filter(key=key).exists():
            NameAlias.objects.update_or_create(key=key, defaults={"entry": entry})


def _import_row(index: int, row: dict, options: ImportOptions, known: dict[str, Genre], result: ImportResult) -> None:
    text = row.get("name", "")
    allowed = VALID_NAME.match(text) and (options.trusted or looks_like_name(text))
    key = canonical_key(text) if allowed else ""
    if not key:
        result.errors.append(f"Line {index}: “{text[:40]}” isn't a name.")
        return
    fields = _fields_from(row, options, key)
    genres = _row_genres(row, options, known)
    entry = CatalogName.objects.filter(key=key).first()
    if entry:
        _merge(entry, fields, genres, options, result)
    else:
        entry = CatalogName.objects.create(**fields)
        entry.genres.set(genres)
        result.created += 1
    _add_variants(entry, row, options)


def import_rows(text: str, options: ImportOptions) -> ImportResult:
    """Accepts a CSV with the `COLUMNS` header, or bare lines of `name, meaning`. Genres split on `|`."""
    result = ImportResult()
    known = {genre.name.lower(): genre for genre in Genre.objects.all()}
    for index, row in enumerate(_rows(text), start=1):
        if index > options.row_limit:
            result.errors.append(f"Stopped after {options.row_limit} lines. Upload the rest separately.")
            break
        if any(row.values()):
            _import_row(index, row, options, known, result)
    return result
