"""Report where affiliation visibility currently relies on the S4E fallback.

Read-only. Writes nothing, changes nothing.

Both user affiliations and document visibility fall back to
``VISIBILITY_DEFAULT`` (``["S4E"]``) when they are empty -- see
``get_user_affiliations`` (documents.py) and ``_normalize_visibility_tags``.
While every member of the consortium is S4E that fallback is invisible, because
defaulting to S4E and being S4E are indistinguishable. Adding a second
institution makes it load-bearing in a way it was never designed to be: an
approved user with no ``UserAffiliation`` record reads the S4E catalogue.

This command measures the blast radius before anyone changes that default, so
the decision is made against counts rather than guesses. Run it before and
after any backfill.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from catalog.documents import (
    AFFILIATION_VALUES,
    Material,
    Recipe,
    UserAffiliation,
    VISIBILITY_DEFAULT,
)


class Command(BaseCommand):
    help = "Report users and documents that depend on the default S4E visibility fallback."

    def add_arguments(self, parser):
        parser.add_argument(
            "--group",
            default=None,
            help="Django group treated as approved. Defaults to settings.APPROVED_GROUP_NAME.",
        )
        parser.add_argument(
            "--list-users",
            action="store_true",
            help="Print the usernames that have no affiliation record, not just the count.",
        )

    def handle(self, *args, **options):
        from django.conf import settings

        group = options["group"] or getattr(settings, "APPROVED_GROUP_NAME", "Approved")

        self.stdout.write(f"Allowed affiliations : {', '.join(AFFILIATION_VALUES)}")
        self.stdout.write(f"Fallback when empty  : {VISIBILITY_DEFAULT}")
        self.stdout.write("")

        self._audit_users(group, list_users=options["list_users"])
        self._audit_documents()

    # -- users ----------------------------------------------------------------

    def _audit_users(self, group, *, list_users):
        User = get_user_model()

        approved = list(User.objects.filter(groups__name=group))
        staff = list(User.objects.filter(is_staff=True) | User.objects.filter(is_superuser=True))
        # Staff and superusers bypass the approval gate entirely, so they reach
        # catalogue views without ever being in the approved group.
        relevant = {u.pk: u for u in approved + staff}

        # One query rather than one per user; these collections stay small but
        # the loop below is O(users) either way and this keeps it off the wire.
        have_record = {
            profile.user_id: list(profile.affiliations or [])
            for profile in UserAffiliation.objects()
        }

        missing = []
        empty = []
        by_affiliation = {}
        for pk, user in relevant.items():
            affiliations = have_record.get(int(pk))
            if affiliations is None:
                missing.append(user.get_username())
            elif not affiliations:
                empty.append(user.get_username())
            else:
                for tag in affiliations:
                    by_affiliation[tag] = by_affiliation.get(tag, 0) + 1

        self.stdout.write(self.style.MIGRATE_HEADING("Users"))
        self.stdout.write(f"  approved group '{group}' : {len(approved)}")
        self.stdout.write(f"  staff / superuser        : {len(staff)}")
        self.stdout.write(f"  distinct users to check  : {len(relevant)}")
        for tag in sorted(by_affiliation):
            self.stdout.write(f"    {tag:<12} {by_affiliation[tag]}")

        total_falling_back = len(missing) + len(empty)
        line = f"  relying on the fallback  : {total_falling_back}"
        self.stdout.write(self.style.ERROR(line) if total_falling_back else self.style.SUCCESS(line))
        if missing:
            self.stdout.write(f"    no UserAffiliation record : {len(missing)}")
            if list_users:
                for name in sorted(missing):
                    self.stdout.write(f"      {name}")
        if empty:
            self.stdout.write(f"    record present but empty  : {len(empty)}")
            if list_users:
                for name in sorted(empty):
                    self.stdout.write(f"      {name}")
        self.stdout.write("")

    # -- documents ------------------------------------------------------------

    def _audit_documents(self):
        self.stdout.write(self.style.MIGRATE_HEADING("Documents"))

        # Material carries ``default_visibility_affiliations``, not
        # ``visibility_affiliations`` -- it has no tag of its own. Browse derives a
        # material's visibility from its recipes and trials; the default is used
        # when creating children and to gate the composition-level download
        # (api_download.py). Querying the wrong field here reports every material
        # as untagged, which is a false positive, not a finding.
        for label, model, field in (
            ("materials", Material, "default_visibility_affiliations"),
            ("recipes", Recipe, "visibility_affiliations"),
        ):
            total = model.objects.count()
            blank = model.objects(
                __raw__={"$or": [{field: {"$exists": False}}, {field: []}]}
            ).count()
            line = f"  {label:<24} {blank} of {total} have no {field}"
            self.stdout.write(self.style.WARNING(line) if blank else self.style.SUCCESS(line))

        # Embedded trials/literature/DFT carry their own visibility list, so a
        # populated parent does not imply a populated child.
        for label, field in (
            ("recipe.trials", "trials"),
            ("recipe.literature", "literature"),
            ("material.dft_calculations", "dft_calculations"),
        ):
            model = Material if field == "dft_calculations" else Recipe
            blank = model.objects(
                __raw__={
                    field: {
                        "$elemMatch": {
                            "$or": [
                                {"visibility_affiliations": {"$exists": False}},
                                {"visibility_affiliations": []},
                            ]
                        }
                    }
                }
            ).count()
            line = f"  {label:<24} {blank} parent docs contain an untagged entry"
            self.stdout.write(self.style.ERROR(line) if blank else self.style.SUCCESS(line))

        self.stdout.write("")
        self.stdout.write(
            "Users counted under the fallback read "
            f"{VISIBILITY_DEFAULT} today; give them an explicit affiliation "
            "before changing the default, or they are locked out. Materials "
            "without a default do not disappear from browse -- their visibility "
            "comes from their recipes and trials -- but they are excluded from "
            "the composition-level download for non-S4E users."
        )
