"""Runs slow third-party work off the request path. A person never waits on an email server, an LLM or Soota.

Jobs live in the database, so they survive restarts. Separate Huey worker containers drain them from Redis queues.
LLM calls have a queue and a worker of their own, so however slow they get, they can't delay a sign-in code. Mail and
Soota share the other, where mail goes first and always has a thread free.
`manage.py run_jobs` picks up anything stranded if a process dies with no traffic to follow.
"""

import logging
import threading
from datetime import timedelta
from importlib import import_module

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from huey.contrib.djhuey import close_db, db_task
from redis.exceptions import RedisError

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
PRIORITIES = {MAIL: 100, SOOTA: 50}
NUDGE_SECONDS = 30
# Priority only orders what's waiting. Without this, slow Soota readings could hold every thread and delay mail.
_slow_slots = threading.BoundedSemaphore(max(settings.HUEY_WORKERS - 1, 1))


@db_task()
def drain_queue(queue: str) -> None:
    if queue == MAIL:
        drain(queue)
        return
    if not _slow_slots.acquire(blocking=False):
        return  # Every slot is already draining, and each keeps going until nothing is left.
    try:
        drain(queue)
    finally:
        _slow_slots.release()


@settings.LLM_HUEY.task()
@close_db
def drain_llm() -> None:
    drain(LLM)


def enqueue(queue: str, kind: str, payload: dict, *, board: Board | None = None, dedupe: str = "") -> Job:
    """With `dedupe`, joins a job already in flight, so fifty people asking for one thing cause one call."""
    if dedupe:
        active = Q(status__in=[Job.Status.QUEUED, Job.Status.RUNNING])
        just_failed = Q(status=Job.Status.FAILED, updated_at__gte=timezone.now() - RECENT_FAILURE)
        existing = Job.objects.filter(active | just_failed, dedupe=dedupe).order_by("-pk").first()
        if existing:
            nudge(queue)
            return existing
    job = Job.objects.create(queue=queue, kind=kind, payload=payload, board=board, dedupe=dedupe)
    kick(queue)
    return Job.objects.filter(pk=job.pk).first() or job  # A private job may already have finished and gone.


def kick(queue: str) -> None:
    if settings.JOBS_EAGER:
        drain(queue)
        return
    transaction.on_commit(lambda: _wake(queue))


def nudge(queue: str) -> None:
    """For pages polling on work already queued, in case its worker has gone. Their every poll needn't be a task."""
    if settings.JOBS_EAGER or cache.add(f"jobs-nudge-{queue}", 1, NUDGE_SECONDS):
        kick(queue)


def kick_after(queue: str, seconds: int) -> None:
    if not settings.JOBS_EAGER:
        _wake(queue, delay=seconds)


def _wake(queue: str, delay: int | None = None) -> None:
    """The job is safe in the database, and `run_jobs` finds it once Redis is back, so an outage never fails a page."""
    try:
        if queue == LLM:
            drain_llm(delay=delay)
        else:
            drain_queue(queue, delay=delay, priority=PRIORITIES[queue])
    except RedisError as exc:
        logger.warning("Couldn't wake the %s queue: %s", queue, type(exc).__name__)


def drain(queue: str) -> None:
    tasks = import_module("names.tasks")
    # People waiting come first, then details for cards, then recordings nobody has asked for yet.
    while _run_next(queue, tasks) or (queue == LLM and (tasks.enrich_batch() or tasks.record_next_voice())):
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
            _wake(job.queue, delay=RETRY_SECONDS * job.attempts)
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
