"""Runs slow third-party work off the request path. A person never waits on an email server or an LLM.

Jobs live in the database, so they survive restarts. Each web process drains them on small background thread
pools, with no extra container to deploy. Email and LLM work have separate pools, so a slow LLM call can't delay
a sign-in code. `manage.py run_jobs` picks up anything stranded if a process dies with no traffic to follow.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import F, Q
from django.utils import timezone

from .ai.base import AIError
from .models import Board, Job

logger = logging.getLogger(__name__)

MAIL, LLM = "mail", "llm"
LEASE = timedelta(minutes=3)  # Longer than any call may take. After this, a claimed job is assumed lost.
RECENT_FAILURE = timedelta(minutes=1)
RETRIES = {"send_email": 5}
RETRY_SECONDS = 30

_pools = {queue: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"jobs-{queue}") for queue in (MAIL, LLM)}
_waiting = {queue: threading.Semaphore(1) for queue in (MAIL, LLM)}


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
    job.refresh_from_db()
    return job


def kick(queue: str) -> None:
    if settings.JOBS_EAGER:
        drain(queue)
        return
    transaction.on_commit(lambda: _schedule(queue))


def _schedule(queue: str) -> None:
    # One drain waiting to start is enough, however many requests kick it while the current one runs.
    if _waiting[queue].acquire(blocking=False):
        _pools[queue].submit(_drain_in_background, queue)


def _drain_in_background(queue: str) -> None:
    _waiting[queue].release()
    close_old_connections()
    try:
        drain(queue)
    except Exception:
        logger.exception("Background jobs stopped unexpectedly")
    finally:
        close_old_connections()


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
    if job.queue == MAIL:
        job.delete()  # Email rows hold addresses and sign-in codes, so they don't stay after sending.
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
            timer = threading.Timer(RETRY_SECONDS * job.attempts + 1, _schedule, [job.queue])
            timer.daemon = True
            timer.start()
        return
    job.status = Job.Status.FAILED
    job.payload = {}
    message = str(exc) if isinstance(exc, AIError) else "That didn't work. Try again in a minute."
    job.result = {"error": message}
    job.save(update_fields=["status", "payload", "result", "updated_at"])


def delete_finished() -> int:
    finished = [Job.Status.DONE, Job.Status.FAILED]
    return Job.objects.filter(status__in=finished, updated_at__lt=timezone.now() - timedelta(days=1)).delete()[0]
