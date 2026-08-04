"""Delete API-key records whose configured expiration has passed."""

from django.core.management.base import BaseCommand

from catalog.api import key_service


class Command(BaseCommand):
    help = "Permanently delete expired LOOP API keys."

    def handle(self, *args, **options):
        deleted = key_service.purge_expired()
        self.stdout.write(f"Deleted {deleted} expired API key(s).")
