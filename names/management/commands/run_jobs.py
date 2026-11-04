from django.core.management.base import BaseCommand

from names import jobs


class Command(BaseCommand):
    help = "Finish background jobs stranded by a restart, and tidy old ones. Safe to run every few minutes."

    def handle(self, *args, **options) -> None:
        removed = jobs.delete_finished()  # First, so a birth-details job past its limit is deleted rather than run.
        for queue in jobs.QUEUES:
            jobs.drain(queue)
        self.stdout.write(f"Removed {removed} finished jobs.")
