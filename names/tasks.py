"""The work jobs.py runs in the background. Nothing here runs while a person is waiting on a page."""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone

from . import catalog, jobs, mail, services, soota
from .ai.base import AIError, AIRateLimited
from .dedup import phonetic_key
from .jobs import drain_queue  # noqa: F401 - Huey worker autodiscovery
from .models import AICall, Board, CatalogName, Job, Name

logger = logging.getLogger(__name__)

MAX_ENRICH_ATTEMPTS = 3
ENRICH_LEASE = timedelta(minutes=3)
MAX_VOICE_ATTEMPTS = 3
VOICE_PAUSED, VOICE_SPACED = "voice-paused", "voice-spaced"


def send_email(job: Job) -> None:
    mail.deliver(job.payload, settings.EMAIL_DELIVERY_BACKEND)


def suggest(job: Job) -> dict:
    board = get_object_or_404(Board, pk=job.payload["board"])
    names = services.suggest_names(board, job.payload.get("more_like", ""))
    return {"names": [name.pk for name in names]}


def family_ideas(job: Job) -> None:
    services.fetch_family_ideas(get_object_or_404(Board, pk=job.payload["board"]))


def speak(job: Job) -> None:
    services.record_audio(Name.objects.get(pk=job.payload["name"]))


def read_birth_star(job: Job) -> None:
    job.board.birth_star = soota.read_birth_star(job.payload["birth"])
    job.board.save(update_fields=["birth_star"])


def read_name_numbers(job: Job) -> None:
    services.store_name_numbers(job.board)


def _waiting():
    lost = Q(enrich_state=CatalogName.Enrich.RUNNING, enrich_claimed_at__lt=timezone.now() - ENRICH_LEASE)
    return CatalogName.objects.filter(Q(enrich_state=CatalogName.Enrich.PENDING) | lost).defer("audio")


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


def enrich_batch() -> bool:
    """Bundles names waiting from every family into one LLM call, then fills in every board that wants them."""
    if not _waiting().exists() or _over_budget():
        return False
    catalog.wait_for_batch()
    batch = _claim_batch()
    if not batch:
        return False
    try:
        ideas = services.get_ai().describe_many([entry.text for entry in batch], catalog.genre_names())
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


def _next_voice() -> CatalogName | None:
    """Names on a board come first. Half the budget at most goes on the catalogue, most picked first, so that a name
    someone adds in the evening doesn't have to wait for tomorrow."""
    day = timezone.now() - timedelta(days=1)
    spent = AICall.objects.filter(user=None, kind=AICall.Kind.SPEAK, created_at__gte=day).count()
    if spent >= settings.AI_VOICE_PER_DAY:
        return None
    # A name still being described may be about to get its native spelling, which is what should be spoken.
    ready = CatalogName.objects.filter(audio__isnull=True).defer("audio").exclude(
        enrich_state__in=[CatalogName.Enrich.PENDING, CatalogName.Enrich.RUNNING]
    )
    lost = Q(voice_state=CatalogName.Enrich.RUNNING, voice_claimed_at__lt=timezone.now() - ENRICH_LEASE)
    waiting = ready.filter(Q(voice_state=CatalogName.Enrich.PENDING) | lost).exclude(status=CatalogName.Status.REJECTED)
    entry = waiting.order_by("pk").first()
    if entry or spent >= settings.AI_VOICE_PER_DAY // 2:
        return entry
    spare = ready.filter(status=CatalogName.Status.APPROVED, voice_state=CatalogName.Enrich.NONE)
    return spare.order_by("-picks", "pk").first()


def _voice_failed(entry: CatalogName) -> None:
    entry.voice_attempts += 1
    give_up = entry.voice_attempts >= MAX_VOICE_ATTEMPTS
    entry.voice_state = CatalogName.Enrich.FAILED if give_up else CatalogName.Enrich.PENDING
    entry.save(update_fields=["voice_attempts", "voice_state"])


def record_next_voice() -> bool:
    """Runs only when nobody is waiting on the LLM queue. False means leave it for now: a later wake carries on."""
    spacing = settings.AI_VOICE_SPACING_SECONDS
    if cache.get(VOICE_PAUSED):
        return False
    entry = _next_voice()
    if entry is None or (spacing and not cache.add(VOICE_SPACED, 1, spacing)):
        return False
    now = timezone.now()
    won = CatalogName.objects.filter(pk=entry.pk, voice_state=entry.voice_state).update(
        voice_state=CatalogName.Enrich.RUNNING, voice_claimed_at=now
    )
    if not won:
        return True
    AICall.objects.create(user=None, kind=AICall.Kind.SPEAK)
    try:
        services.record_catalog_voice(entry)
    except AIRateLimited:
        logger.warning("Pausing voice recordings: rate limited")
        CatalogName.objects.filter(pk=entry.pk).update(voice_state=CatalogName.Enrich.PENDING)
        cache.set(VOICE_PAUSED, 1, settings.AI_VOICE_PAUSE_SECONDS)
        return False  # `run_jobs` carries on once the pause has passed.
    except Exception as exc:  # noqa: BLE001 - One bad name must never stop the queue.
        logger.warning("Voice recording failed: %s", type(exc).__name__)
        _voice_failed(entry)
    if spacing:
        jobs.kick_after(jobs.LLM, spacing)
        return False
    return True
