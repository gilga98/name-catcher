"""Asks Soota's calculation service for a baby's birth star. Birth details go there and nowhere else.

We keep what Soota works out, not what it was told. Its natal answer holds a whole chart, and we read only the Moon's
star and the naming sounds that go with it, then the numbers for the date. Numerology gets only what it needs: a name
number is worked out from the name alone, so nothing about the family goes with it.
"""

import ipaddress
import logging
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
_IST_HOURS = 5.5
MAX_NAMES = 50  # What Soota takes in one request.
# The star follows the Moon, which looks the same from anywhere in India, so the nearest large city is close enough.
PLACES = {
    "agartala": ("Agartala", 23.83, 91.29),
    "ahmedabad": ("Ahmedabad", 23.02, 72.57),
    "aizawl": ("Aizawl", 23.73, 92.72),
    "amritsar": ("Amritsar", 31.63, 74.87),
    "bengaluru": ("Bengaluru", 12.97, 77.59),
    "bhopal": ("Bhopal", 23.26, 77.41),
    "bhubaneswar": ("Bhubaneswar", 20.30, 85.82),
    "chandigarh": ("Chandigarh", 30.73, 76.78),
    "chennai": ("Chennai", 13.08, 80.27),
    "coimbatore": ("Coimbatore", 11.02, 76.96),
    "dehradun": ("Dehradun", 30.32, 78.03),
    "delhi": ("Delhi", 28.61, 77.21),
    "gangtok": ("Gangtok", 27.33, 88.61),
    "guwahati": ("Guwahati", 26.14, 91.74),
    "hyderabad": ("Hyderabad", 17.39, 78.49),
    "imphal": ("Imphal", 24.82, 93.94),
    "indore": ("Indore", 22.72, 75.86),
    "jaipur": ("Jaipur", 26.91, 75.79),
    "jammu": ("Jammu", 32.73, 74.86),
    "kochi": ("Kochi", 9.93, 76.27),
    "kohima": ("Kohima", 25.67, 94.11),
    "kolkata": ("Kolkata", 22.57, 88.36),
    "leh": ("Leh", 34.15, 77.58),
    "lucknow": ("Lucknow", 26.85, 80.95),
    "madurai": ("Madurai", 9.93, 78.12),
    "mumbai": ("Mumbai", 19.08, 72.88),
    "nagpur": ("Nagpur", 21.15, 79.09),
    "panaji": ("Panaji", 15.49, 73.83),
    "patna": ("Patna", 25.59, 85.14),
    "port-blair": ("Port Blair", 11.62, 92.73),
    "pune": ("Pune", 18.52, 73.86),
    "raipur": ("Raipur", 21.25, 81.63),
    "ranchi": ("Ranchi", 23.34, 85.31),
    "shillong": ("Shillong", 25.58, 91.89),
    "shimla": ("Shimla", 31.10, 77.17),
    "srinagar": ("Srinagar", 34.08, 74.80),
    "surat": ("Surat", 21.17, 72.83),
    "thiruvananthapuram": ("Thiruvananthapuram", 8.52, 76.94),
    "varanasi": ("Varanasi", 25.32, 82.97),
    "vijayawada": ("Vijayawada", 16.51, 80.65),
    "visakhapatnam": ("Visakhapatnam", 17.69, 83.22),
}
PLACE_CHOICES = [(slug, place[0]) for slug, place in PLACES.items()]


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


def born_at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=IST)


def birth_input(day: date, clock: time, place: str) -> dict:
    """Soota's BirthInput, field for field. It refuses anything extra."""
    _label, latitude, longitude = PLACES[place]
    return {
        "dob": day.isoformat(),
        "tob": clock.strftime("%H:%M:%S"),
        "latitude": latitude,
        "longitude": longitude,
        "timezone_offset": _IST_HOURS,
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
