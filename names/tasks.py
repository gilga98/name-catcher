"""The work jobs.py runs in the background. Nothing here runs while a person is waiting on a page."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone

from . import catalog, mail, services
from .ai.base import AIError
from .dedup import phonetic_key
from .models import AICall, Board, CatalogName, Job, Name

MAX_ENRICH_ATTEMPTS = 3
ENRICH_LEASE = timedelta(minutes=3)


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
