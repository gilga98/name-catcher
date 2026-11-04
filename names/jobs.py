"""Runs slow third-party work off the request path. A person never waits on an email server, an LLM or Soota.

Jobs live in the database, so they survive restarts. A separate Huey worker container drains them from Redis queues.
Work is prioritized so a slow LLM call can't delay a sign-in code.
`manage.py run_jobs` picks up anything stranded if a process dies with no traffic to follow.
"""

import logging
from datetime import timedelta
from importlib import import_module

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from huey.contrib.djhuey import db_task

from .ai.base import AIError
from .models import Board, Job

logger = logging.getLogger(__name__)

MAIL, LLM, SOOTA = "mail", "llm", "soota"
QUEUES = (MAIL, LLM, SOOTA)
# These rows hold addresses, sign-in codes or birth details, so they don't stay once the work is done.
PRIVATE_QUEUES = (MAIL, SOOTA)
LEASE = timedelta(minutes=3)  # Longer than any call may take. After this, a claimed job is assumed lost.
RECENT_FAILURE = timedelta(minutes=1)
SOOTA_GIVES_UP_AFTER = timedelta(hours=1)
RETRIES = {"send_email": 5, "read_birth_star": 2}
RETRY_SECONDS = 30
PRIORITIES = {MAIL: 100, SOOTA: 50, LLM: 10}


@db_task()
def drain_queue(queue: str) -> None:
    drain(queue)


def enqueue(queue: str, kind: str, payload: dict, *, board: Board | None = None, dedupe: str = "") -> Job:
    """With `dedupe`, joins a job already in flight, so fifty people asking for one thing cause one call."""
    if dedupe:
        active = Q(status__in=[Job.Status.QUEUED, Job.Status.RUNNING])
        just_failed = Q(status=Job.Status.FAILED, updated_at__gte=timezone.now() - RECENT_FAILURE)
        existing = Job.objects.filter(active | just_failed, dedupe=dedupe).order_by("-pk").first()
        if existing:
            kick(queue)
            return existing
    job = Job.objects.create(queue=queue, kind=kind, payload=payload, board=board, dedupe=dedupe)
    kick(queue)
    return Job.objects.filter(pk=job.pk).first() or job  # A private job may already have finished and gone.


def kick(queue: str) -> None:
    if settings.JOBS_EAGER:
        drain(queue)
        return
    priority = PRIORITIES.get(queue, 0)
    transaction.on_commit(lambda: drain_queue(queue, priority=priority))


def drain(queue: str) -> None:
    tasks = import_module("names.tasks")
    while _run_next(queue, tasks) or (queue == LLM and tasks.enrich_batch()):
        pass


def _claim(queue: str) -> Job | None:
    now = timezone.now()
    Job.objects.filter(queue=queue, status=Job.Status.RUNNING, claimed_at__lt=now - LEASE).update(
        status=Job.Status.QUEUED
    )
    job = Job.objects.filter(queue=queue, status=Job.Status.QUEUED, run_after__lte=now).order_by("pk").first()
    if job is None:
        return None
    # Another process may be after the same job, and only one update can win.
    won = Job.objects.filter(pk=job.pk, status=Job.Status.QUEUED).update(
        status=Job.Status.RUNNING, claimed_at=now, attempts=F("attempts") + 1
    )
    if not won:
        return _claim(queue)
    job.refresh_from_db()
    return job


def _run_next(queue: str, tasks) -> bool:
    job = _claim(queue)
    if job is None:
        return False
    try:
        result = getattr(tasks, job.kind)(job)
    except Exception as exc:  # noqa: BLE001 - One bad job must never stop the rest.
        _handle_failure(job, exc)
        return True
    if job.queue in PRIVATE_QUEUES:
        job.delete()
        return True
    job.status, job.result = Job.Status.DONE, result or {}
    job.save(update_fields=["status", "result", "updated_at"])
    return True


def _handle_failure(job: Job, exc: Exception) -> None:
    # The type only: exception text from a mail server or SDK can contain addresses or keys.
    logger.warning("Job %s failed: %s", job.kind, type(exc).__name__)
    if job.attempts < RETRIES.get(job.kind, 1):
        job.status = Job.Status.QUEUED
        job.run_after = timezone.now() + timedelta(seconds=RETRY_SECONDS * job.attempts)
        job.save(update_fields=["status", "run_after", "updated_at"])
        if not settings.JOBS_EAGER:
            drain_queue.schedule(
                args=[job.queue],
                delay=RETRY_SECONDS * job.attempts,
                priority=PRIORITIES.get(job.queue, 0),
            )
        return
    job.status = Job.Status.FAILED
    job.payload = {}
    message = str(exc) if isinstance(exc, AIError) else "That didn't work. Try again in a minute."
    job.result = {"error": message}
    job.save(update_fields=["status", "payload", "result", "updated_at"])


def delete_finished() -> int:
    """Birth details don't wait for an outage to end: past the limit the parent can simply ask again."""
    finished = [Job.Status.DONE, Job.Status.FAILED]
    old = Q(status__in=finished, updated_at__lt=timezone.now() - timedelta(days=1))
    stuck = Q(queue=SOOTA, created_at__lt=timezone.now() - SOOTA_GIVES_UP_AFTER)
    return Job.objects.filter(old | stuck).delete()[0]
