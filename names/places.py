"""Finds a place of birth, and the coordinates and time zone Soota needs for it.

Searches go through our server, so Open-Meteo sees what was typed but not who typed it. A place reaches the form as a
signed value, so a reading can only use coordinates and a time zone that a search really returned.
"""

import hashlib
import logging
import unicodedata
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from django.core import signing
from django.core.cache import cache

logger = logging.getLogger(__name__)

SEARCH_URL = "https://geocoding-api.open-meteo.com/v1/search"
MIN_QUERY, MAX_QUERY = 2, 60
MAX_OPTIONS = 8
_FETCHED = 20
_TIMEOUT_SECONDS = 4.0
_CACHE_SECONDS = 24 * 60 * 60
_SIGNING_SALT = "names.places"
_SIGNED_FOR = timedelta(days=1)
_SAME_PLACE_DEGREES = 0.25  # About 25 km. Closer than this, two rows with one name are one town.
_INDIA = "IN"
# Open-Meteo doesn't know these names, and answers with somewhere else: Bombay is in New York, Madras in Oregon.
FORMER_NAMES = {
    "alleppey": "Alappuzha",
    "allahabad": "Prayagraj",
    "banaras": "Varanasi",
    "bangalore": "Bengaluru",
    "belgaum": "Belagavi",
    "benares": "Varanasi",
    "benaras": "Varanasi",
    "bombay": "Mumbai",
    "calcutta": "Kolkata",
    "calicut": "Kozhikode",
    "cochin": "Kochi",
    "gulbarga": "Kalaburagi",
    "gurgaon": "Gurugram",
    "hubli": "Hubballi",
    "madras": "Chennai",
    "mangalore": "Mangaluru",
    "mysore": "Mysuru",
    "pondicherry": "Puducherry",
    "pondy": "Puducherry",
    "quilon": "Kollam",
    "tanjore": "Thanjavur",
    "trichur": "Thrissur",
    "trichy": "Tiruchirappalli",
    "trivandrum": "Thiruvananthapuram",
}


class PlaceSearchError(Exception):
    """Open-Meteo couldn't answer. Nothing here is meant for the person."""


class BadPlace(Exception):
    """This isn't a place one of our searches returned."""


def _fold(text: str | None) -> str:
    """Rāmpur and Rampur are one spelling to someone reading a list."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold().strip()


def _fetch(name: str) -> list[dict]:
    params = {"name": name, "count": _FETCHED, "language": "en", "format": "json"}
    try:
        response = httpx.get(SEARCH_URL, params=params, timeout=_TIMEOUT_SECONDS, trust_env=False)
        response.raise_for_status()
        rows = response.json().get("results", [])
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.warning("Place search failed: %s", type(exc).__name__)  # Exception text could hold what was typed.
        raise PlaceSearchError(type(exc).__name__) from exc
    return rows if isinstance(rows, list) else []


def _knows_time_zone(name: str | None) -> bool:
    try:
        ZoneInfo(name or "")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _usable(row: dict) -> bool:
    """Towns and cities only, not mountains or airports. With no time zone we'd be guessing, so the row goes."""
    return (
        str(row.get("feature_code", "")).startswith("PPL")
        and bool(row.get("name"))
        and isinstance(row.get("latitude"), int | float)
        and isinstance(row.get("longitude"), int | float)
        and _knows_time_zone(row.get("timezone"))
    )


def _town(row: dict) -> tuple[str, str, str]:
    return _fold(row["name"]), _fold(row.get("admin1")), _fold(row.get("country"))


def _same_place(row: dict, other: dict) -> bool:
    if _town(row) != _town(other):
        return False
    if _fold(row.get("admin2")) == _fold(other.get("admin2")):
        return True
    return (
        abs(row["latitude"] - other["latitude"]) < _SAME_PLACE_DEGREES
        and abs(row["longitude"] - other["longitude"]) < _SAME_PLACE_DEGREES
    )


def _distinct(rows: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for row in rows:
        if not any(_same_place(row, other) for other in kept):
            kept.append(row)
    return kept


def _detail(row: dict, kept: list[dict]) -> str:
    """The district only appears when it's what tells two towns of one name apart."""
    twins = sum(_town(row) == _town(other) for other in kept) > 1
    district = row.get("admin2") if twins and _fold(row.get("admin2")) != _fold(row["name"]) else None
    return ", ".join(part for part in (district, row.get("admin1"), row.get("country")) if part)


def sign(place: dict) -> str:
    return signing.dumps(place, salt=_SIGNING_SALT)


def read(value: str) -> dict:
    try:
        return signing.loads(value, salt=_SIGNING_SALT, max_age=_SIGNED_FOR)
    except signing.BadSignature as exc:
        raise BadPlace from exc


def _option(row: dict, kept: list[dict]) -> dict:
    detail = _detail(row, kept)
    place = {
        "label": ", ".join(part for part in (row["name"], detail) if part),
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "timezone": row["timezone"],
    }
    return {"value": sign(place), "name": row["name"], "detail": detail, "timezone": row["timezone"]}


def _ranked(rows: list[dict], query: str) -> list[dict]:
    """The name they typed comes first, and for families in India, the one in India. Otherwise Open-Meteo's order."""
    wanted = _fold(query)
    return sorted(rows, key=lambda row: (_fold(row["name"]) != wanted, row.get("country_code") != _INDIA))


def _current_name_rows(query: str) -> list[dict]:
    current = FORMER_NAMES.get(_fold(query))
    if current is None:
        return []
    rows = [row for row in _fetch(current) if row.get("country_code") == _INDIA]
    return [row for row in rows if _fold(row.get("name")) == _fold(current)]


def _lookup(query: str) -> list[dict]:
    rows = [row for row in [*_current_name_rows(query), *_fetch(query)] if _usable(row)]
    current_first = FORMER_NAMES.get(_fold(query), query)
    kept = _distinct(_ranked(rows, current_first))[:MAX_OPTIONS]
    return [_option(row, kept) for row in kept]


def search(query: str) -> list[dict]:
    """Options to show, best first. No two read the same. Searches are cached, so a popular town costs one call."""
    query = " ".join(query.split())[:MAX_QUERY]
    if len(query) < MIN_QUERY:
        return []
    key = f"places:{hashlib.sha256(_fold(query).encode()).hexdigest()}"
    options = cache.get(key)
    if options is None:
        options = _lookup(query)
        cache.set(key, options, _CACHE_SECONDS)
    return options


def local_moment(day: date, clock: time, place: dict) -> datetime:
    return datetime.combine(day, clock, tzinfo=ZoneInfo(place["timezone"]))


def utc_offset_hours(day: date, clock: time, place: dict) -> float:
    """For that place on that date and at that time, because clocks there may have been set differently then."""
    return round(local_moment(day, clock, place).utcoffset().total_seconds() / 3600, 4)
