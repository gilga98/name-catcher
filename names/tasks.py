"""The work jobs.py runs in the background. Nothing here runs while a person is waiting on a page."""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, OuterRef, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone

from . import catalog, jobs, mail, services, soota
from .ai.base import AIError, AIRateLimited
from .ai.rotation import resting
from .dedup import phonetic_key
from .jobs import drain_queue  # noqa: F401 - Huey worker autodiscovery
from .models import AICall, Board, CatalogName, Job, Name

logger = logging.getLogger(__name__)

MAX_ENRICH_ATTEMPTS = 3
ENRICH_LEASE = timedelta(minutes=3)
MAX_RESPELL_ATTEMPTS = 3
RESPELL_PAUSED, RESPELL_SPACED = "respell-paused", "respell-spaced"


def send_email(job: Job) -> None:
    mail.deliver(job.payload, settings.EMAIL_DELIVERY_BACKEND)


def suggest(job: Job) -> dict:
    board = get_object_or_404(Board, pk=job.payload["board"])
    names, from_catalog = services.suggest_or_close_matches(
        board, job.payload.get("more_like", ""), job.payload.get("charge")
    )
    return {"names": [name.pk for name in names], "from_catalog": from_catalog}


def family_ideas(job: Job) -> None:
    board = get_object_or_404(Board, pk=job.payload["board"])
    services.fetch_family_ideas(board, refresh=job.payload.get("refresh", False))


def read_birth_star(job: Job) -> None:
    job.board.birth_star = soota.read_birth_star(job.payload["birth"])
    job.board.save(update_fields=["birth_star"])


def read_name_numbers(job: Job) -> None:
    services.store_name_numbers(job.board)


def _waiting():
    lost = Q(enrich_state=CatalogName.Enrich.RUNNING, enrich_claimed_at__lt=timezone.now() - ENRICH_LEASE)
    return CatalogName.objects.filter(Q(enrich_state=CatalogName.Enrich.PENDING) | lost)


def _over_budget() -> bool:
    """Tagging isn't charged to any one family, so this is what stops a flood of made-up names running up a bill."""
    recent = AICall.objects.filter(created_at__gte=timezone.now() - timedelta(hours=1))
    names = recent.aggregate(total=Sum("units"))["total"] or 0
    calls = recent.filter(kind=AICall.Kind.DESCRIBE).count()
    return names >= settings.AI_ENRICH_NAMES_PER_HOUR or calls >= settings.AI_ENRICH_CALLS_PER_HOUR


def _next_batch() -> list[CatalogName]:
    """Only names a person has approved share a prompt. Otherwise, one family's text could steer another's answer."""
    waiting = _waiting().order_by("updated_at")
    first = waiting.first()
    if first is None or first.status != CatalogName.Status.APPROVED:
        return [first] if first else []
    return list(waiting.filter(status=CatalogName.Status.APPROVED)[: settings.AI_ENRICH_BATCH_SIZE])


def _claim_batch() -> list[CatalogName]:
    now = timezone.now()
    batch = []
    for entry in _next_batch():
        won = CatalogName.objects.filter(pk=entry.pk, enrich_state=entry.enrich_state).update(
            enrich_state=CatalogName.Enrich.RUNNING, enrich_claimed_at=now
        )
        if won:
            batch.append(entry)
    return batch


def _give_back(entries: list[CatalogName]) -> None:
    for entry in entries:
        entry.enrich_attempts += 1
        entry.enrich_state = (
            CatalogName.Enrich.FAILED
            if entry.enrich_attempts >= MAX_ENRICH_ATTEMPTS
            else CatalogName.Enrich.PENDING
        )
        entry.save(update_fields=["enrich_attempts", "enrich_state", "updated_at"])


def _put_back(entries: list[CatalogName]) -> None:
    """Nobody could answer, which says nothing about these names, so it doesn't count against them."""
    pks = [entry.pk for entry in entries]
    CatalogName.objects.filter(pk__in=pks).update(enrich_state=CatalogName.Enrich.PENDING)


def enrich_batch() -> bool:
    """Bundles names waiting from every family into one LLM call, then fills in every board that wants them."""
    if resting() or not _waiting().exists() or _over_budget():
        return False
    catalog.wait_for_batch()
    batch = _claim_batch()
    if not batch:
        return False
    try:
        ideas = services.get_ai().describe_many([entry.text for entry in batch], catalog.genre_names())
    except AIRateLimited:
        _put_back(batch)  # `run_jobs` carries on once AI is back.
        return False
    except AIError:
        _give_back(batch)
        return False
    AICall.objects.create(user=None, kind=AICall.Kind.DESCRIBE, units=len(batch))
    answers = {phonetic_key(idea.given): catalog.tidy_idea(idea) for idea in ideas}
    missing = []
    for entry in batch:
        idea = answers.get(phonetic_key(entry.text))
        if idea is None:
            missing.append(entry)
        else:
            services.fill_boards(catalog.apply_idea(entry, idea))
    _give_back(missing)
    return True


def _lacking_sounds_like():
    being_described = [CatalogName.Enrich.PENDING, CatalogName.Enrich.RUNNING]
    lacking = CatalogName.objects.filter(sounds_like="", sounds_like_attempts__lt=MAX_RESPELL_ATTEMPTS)
    lacking = lacking.exclude(meaning="").exclude(status=CatalogName.Status.REJECTED)
    lacking = lacking.exclude(enrich_state__in=being_described)
    return lacking.annotate(on_board=Exists(Name.objects.filter(key=OuterRef("key"))))


def _next_respelling() -> list[CatalogName]:
    """Names on a board first, then the most picked. As with details, only approved names share a prompt."""
    lacking = _lacking_sounds_like().order_by("-on_board", "-picks", "pk")
    first = lacking.first()
    if first is None or first.status != CatalogName.Status.APPROVED:
        return [first] if first else []
    return list(lacking.filter(status=CatalogName.Status.APPROVED)[: settings.AI_ENRICH_BATCH_SIZE])


def _store_sounds_like(entry: CatalogName, spelling: str) -> None:
    """A spelling that doesn't pass counts against the name, so one the LLM can't manage stops costing calls."""
    entry.sounds_like = catalog.checked_sounds_like(spelling, entry.text)
    entry.sounds_like_attempts += 1
    entry.save(update_fields=["sounds_like", "sounds_like_attempts"])
    if entry.sounds_like:
        Name.objects.filter(key=entry.key, sounds_like="").update(sounds_like=entry.sounds_like)


def respell_batch() -> bool:
    """Runs only when nothing else is waiting. False means leave it for now: a later wake carries on."""
    spacing = settings.AI_RESPELL_SPACING_SECONDS
    day = timezone.now() - timedelta(days=1)
    spent = AICall.objects.filter(kind=AICall.Kind.RESPELL, created_at__gte=day).count()
    if resting() or cache.get(RESPELL_PAUSED) or spent >= settings.AI_RESPELL_CALLS_PER_DAY:
        return False
    batch = _next_respelling()
    if not batch or (spacing and not cache.add(RESPELL_SPACED, 1, spacing)):
        return False
    AICall.objects.create(user=None, kind=AICall.Kind.RESPELL, units=len(batch))
    try:
        answers = services.get_ai().respell_many([entry.text for entry in batch])
    except AIRateLimited:
        logger.warning("Pausing respelling: rate limited")
        cache.set(RESPELL_PAUSED, 1, settings.AI_RESPELL_PAUSE_SECONDS)
        return False  # `run_jobs` carries on once the pause has passed.
    except AIError:
        answers = []
    spellings = {answer.given: answer.sounds_like for answer in answers}
    for entry in batch:
        _store_sounds_like(entry, spellings.get(entry.text, ""))
    if spacing:
        jobs.kick_after(jobs.LLM, spacing)
        return False
    return True
