from django.conf import settings
from django.core.management.base import BaseCommand
from huey.consumer_options import ConsumerConfig

from names import jobs  # noqa: F401 - Registers the task the consumer runs.


class Command(BaseCommand):
    help = "Run the Huey consumer for LLM calls. They have their own queue, so they can't delay a sign-in code."

    def handle(self, *args, **options) -> None:
        config = ConsumerConfig(workers=settings.LLM_WORKERS, worker_type="thread", periodic=False)
        config.validate()
        settings.LLM_HUEY.create_consumer(**config.values).run()
