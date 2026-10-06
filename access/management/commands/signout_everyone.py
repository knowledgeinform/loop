"""Sign every account out, so each fills in its profile at the next sign-in.

Used once when the profile and the usage notice go live (decided by Corey
Oses, 2026-09-29: every account, his included). Deletes all database
sessions; API keys are not touched (ProfileComplete refuses them until their
owner has filled in the profile).

    python manage.py signout_everyone --yes
"""

from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Delete every session, signing all accounts out."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="really do it")

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("this signs everyone out; run again with --yes")
        count, _ = Session.objects.all().delete()
        self.stdout.write(f"signed out: {count} sessions deleted")
