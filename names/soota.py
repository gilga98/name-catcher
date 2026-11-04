"""Asks Soota's calculation service for a baby's birth star. Birth details go there and nowhere else.

We keep what Soota works out, not what it was told. Its natal answer holds a whole chart, and we read only the Moon's
star and the naming sounds that go with it, then the numbers for the date. Numerology gets only what it needs: a name
number is worked out from the name alone, so nothing about the family goes with it.
"""

import ipaddress
import logging
from datetime import date, time
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from pydantic import BaseModel, Field, ValidationError

from . import places

logger = logging.getLogger(__name__)

MAX_NAMES = 50  # What Soota takes in one request.

class SootaError(Exception):
    """Soota couldn't answer. The text never holds birth details, and isn't meant for the person."""


class _Avakahada(BaseModel):
    nakshatra: str = Field(min_length=1, max_length=40)
    pada: int = Field(ge=1, le=4)
    sign: str = Field(min_length=1, max_length=20)
    nama_letter: str = Field(min_length=1, max_length=10)
    nama_letters: str = Field(min_length=1, max_length=60)


class _Natal(BaseModel):
    avakahada: _Avakahada


class _NameNumber(BaseModel):
    name: str
    total: int = Field(ge=1)
    number: int = Field(ge=1, le=9)
    planet: str = Field(min_length=1, max_length=20)


class _BirthNumbers(BaseModel):
    birth_number: int = Field(ge=1, le=9)
    destiny_number: int = Field(ge=1, le=9)


class _Numerology(BaseModel):
    names: list[_NameNumber]
    birth: _BirthNumbers | None = None


def birth_input(day: date, clock: time, place: dict) -> dict:
    """Soota's BirthInput, field for field. It refuses anything extra."""
    return {
        "dob": day.isoformat(),
        "tob": clock.strftime("%H:%M:%S"),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "timezone_offset": places.utc_offset_hours(day, clock, place),
        "ayanamsha": settings.SOOTA_AYANAMSHA,
        "use_true_node": False,
    }


def _is_private_host(host: str) -> bool:
    """A private address, or a Docker service name like soota-engine-gateway. Public names always hold a dot."""
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return bool(host) and "." not in host


def _url(path: str) -> str:
    url = settings.SOOTA_API_URL.rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" and not _is_private_host(parts.hostname or ""):
        logger.error("SOOTA_API_URL must be a private address, as birth details travel over it unencrypted.")
        raise SootaError("Refused an unencrypted public address.")
    return f"{url}{path}"


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.SOOTA_API_KEY}"} if settings.SOOTA_API_KEY else {}


def _post(path: str, body: dict, model: type[BaseModel]) -> BaseModel:
    url = _url(path)
    try:
        response = httpx.post(
            url, json=body, headers=_headers(), timeout=settings.SOOTA_TIMEOUT_SECONDS, trust_env=False
        )
        response.raise_for_status()
        return model.model_validate_json(response.content)
    except httpx.HTTPStatusError as exc:
        logger.warning("Soota answered %s", exc.response.status_code)  # 401: wrong key. 429: too many at once.
        raise SootaError(type(exc).__name__) from exc
    except (httpx.HTTPError, ValidationError) as exc:
        raise SootaError(type(exc).__name__) from exc


def _birth_numbers(birth_date: str) -> _BirthNumbers:
    body = {"names": [], "system": settings.SOOTA_NUMEROLOGY_SYSTEM, "birth_date": birth_date}
    numbers = _post("/v1/numerology", body, _Numerology).birth
    if numbers is None:
        raise SootaError("No birth numbers in the answer.")
    return numbers


def read_birth_star(birth: dict) -> dict:
    """Takes a `birth_input`. Returns only what's safe to keep: the star, its sounds and two digits, not the birth."""
    star = _post("/v1/natal", {"birth": birth}, _Natal).avakahada
    numbers = _birth_numbers(birth["dob"])
    return {
        "nakshatra": star.nakshatra,
        "pada": star.pada,
        "sign": star.sign,
        "letter": star.nama_letter,
        "letters": [letter.strip() for letter in star.nama_letters.split(",") if letter.strip()],
        "birth_number": numbers.birth_number,
        "destiny_number": numbers.destiny_number,
    }


def read_name_numbers(names: list[str]) -> list[dict]:
    """In the order given. `text` records the spelling each number belongs to, since another spelling has another."""
    body = {"names": names, "system": settings.SOOTA_NUMEROLOGY_SYSTEM}
    rows = _post("/v1/numerology", body, _Numerology).names
    if [row.name for row in rows] != names:
        raise SootaError("The answer doesn't match the names asked about.")
    return [{"text": row.name, "total": row.total, "number": row.number, "planet": row.planet} for row in rows]
