from django.core.management.base import BaseCommand

from names import jobs


class Command(BaseCommand):
    help = "Finish background jobs stranded by a restart, and tidy old ones. Safe to run every few minutes."

    def handle(self, *args, **options) -> None:
        for queue in (jobs.MAIL, jobs.LLM):
            jobs.drain(queue)
        self.stdout.write(f"Removed {jobs.delete_finished()} finished jobs.")
