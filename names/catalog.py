"""The shared name pool: what we can answer without an LLM, and how people and admins grow it."""

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from django.conf import settings
from django.db.models import Count, F, Max, Q

from .ai.base import NameIdea
from .dedup import phonetic_key
from .models import CatalogName, Genre

VALID_NAME = re.compile(r"^[^\d<>/:@]{1,40}$")
COLUMNS = [
    "name", "meaning", "origin", "gender", "genres", "native", "respelling", "speech_lang", "rarity", "style",
    "insight",
]  # fmt: skip
# The names an LLM reaches for first, whatever the brief. Popularity from our own boards is added to these.
ALWAYS_OVERUSED = (
    "Aarav", "Vivaan", "Vihaan", "Aditya", "Arjun", "Reyansh", "Ayaan", "Krishna", "Ishaan", "Kiaan",
    "Aadhya", "Ananya", "Diya", "Saanvi", "Myra", "Kiara", "Anaya", "Pari", "Aarohi", "Navya",
)  # fmt: skip
_VOWELS = "aeiou"
_MEANING_WORDS, _INSIGHT_WORDS = 12, 20
_MARKUP = re.compile(r"[*_`#]+")
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


def lookup(key: str) -> CatalogName | None:
    """A known answer for this name. Unreviewed LLM text is no worse than asking again; user text waits."""
    trusted = Q(status=CatalogName.Status.APPROVED) | Q(
        status=CatalogName.Status.PENDING, source=CatalogName.Source.LLM
    )
    return CatalogName.objects.filter(trusted, key=key).exclude(meaning="").first()


def overused_names() -> list[str]:
    popular = _approved().filter(picks__gt=0).order_by("-picks").values_list("text", flat=True)[:30]
    return sorted({*ALWAYS_OVERUSED, *popular})


def themes_from_text(text: str) -> list[str]:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return sorted(genre for genre, vocabulary in _GENRE_WORDS.items() if words & set(vocabulary.split()))


def tidy(text: str, max_words: int) -> str:
    """Models ignore length limits often enough that the limit has to be enforced here as well."""
    words = _MARKUP.sub("", text).split()
    return " ".join(words[:max_words]).rstrip(",;:")


def tidy_idea(idea: NameIdea) -> NameIdea:
    return idea.model_copy(
        update={
            "meaning": tidy(idea.meaning, _MEANING_WORDS),
            "insight": tidy(idea.insight, _INSIGHT_WORDS)[:200],
            "surname_fit": tidy(idea.surname_fit, _INSIGHT_WORDS)[:200],
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
    """A plain observation about how the two names sound together. Needs no LLM and never leaves the server."""
    first, last = text.strip().lower(), surname.strip().lower()
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
        CatalogName.objects.filter(pk=entry.pk).update(submissions=F("submissions") + 1)
        result.duplicates += 1
        return
    for name in ("meaning", "insight", "origin", "native", "respelling", "speech_lang"):
        if fields[name] and not getattr(entry, name):
            setattr(entry, name, fields[name])
    entry.status = options.status
    entry.save()
    entry.genres.add(*genres)
    result.merged += 1


def _import_row(index: int, row: dict, options: ImportOptions, known: dict[str, Genre], result: ImportResult) -> None:
    text = row.get("name", "")
    key = phonetic_key(text)[:60] if VALID_NAME.match(text) else ""
    if not key:
        result.errors.append(f"Line {index}: “{text[:40]}” isn't a name.")
        return
    fields = _fields_from(row, options, key)
    genres = _row_genres(row, options, known)
    entry = CatalogName.objects.filter(key=key).first()
    if entry:
        _merge(entry, fields, genres, options, result)
        return
    entry = CatalogName.objects.create(**fields)
    entry.genres.set(genres)
    result.created += 1


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
