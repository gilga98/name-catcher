from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from names.accounts import delete_account


class Command(BaseCommand):
    help = "Delete accounts idle past ACCOUNT_RETENTION_DAYS, as the Privacy Policy promises. Run daily."

    def handle(self, *args, **options) -> None:
        cutoff = timezone.now() - timedelta(days=settings.ACCOUNT_RETENTION_DAYS)
        idle = Q(last_login__lt=cutoff) | Q(last_login__isnull=True, date_joined__lt=cutoff)
        users = get_user_model().objects.filter(idle, is_staff=False)
        count = 0
        for user in users:
            delete_account(user)
            count += 1
        self.stdout.write(f"Deleted {count} idle accounts.")
