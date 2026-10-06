"""Accounts that confirmed their address before this app existed.

The activation link has always put accounts in the ``email_confirmed`` group.
Their current address is taken as the verified one (users cannot change their
address in LOOP; only an admin can), dated to when the account was created.
"""

from django.conf import settings
from django.db import migrations


def backfill(apps, schema_editor):
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    VerifiedEmail = apps.get_model("access", "VerifiedEmail")
    for user in User.objects.filter(groups__name="email_confirmed").exclude(email=""):
        VerifiedEmail.objects.get_or_create(
            user=user,
            email=user.email.strip().lower(),
            defaults={"verified_at": user.date_joined},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("access", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
