from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="VerifiedEmail",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254)),
                ("verified_at", models.DateTimeField()),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="verified_emails",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-verified_at"]},
        ),
        migrations.AddConstraint(
            model_name="verifiedemail",
            constraint=models.UniqueConstraint(fields=("user", "email"), name="access_verified_email_once"),
        ),
        migrations.CreateModel(
            name="TermsAcceptance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document", models.CharField(max_length=20)),
                ("version", models.CharField(max_length=40)),
                ("accepted_at", models.DateTimeField(auto_now_add=True)),
                ("ip", models.CharField(blank=True, max_length=64)),
                ("user_agent", models.CharField(blank=True, max_length=300)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="terms_acceptances",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-accepted_at"]},
        ),
        migrations.AddConstraint(
            model_name="termsacceptance",
            constraint=models.UniqueConstraint(
                fields=("user", "document", "version"), name="access_terms_once_per_version"
            ),
        ),
        migrations.CreateModel(
            name="AccessRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("resource", models.CharField(choices=[("chaos", "CHAOS")], default="chaos", max_length=20)),
                ("affiliation", models.CharField(max_length=200)),
                ("purpose", models.TextField(max_length=2000)),
                ("email_at_request", models.EmailField(max_length=254)),
                ("terms_version", models.CharField(max_length=40)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("denied", "Denied"),
                            ("revoked", "Revoked"),
                        ],
                        default="pending",
                        max_length=10,
                    ),
                ),
                (
                    "decided_via",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("verified_email", "Verified address with an allowed ending"),
                            ("loop_approved", "Approved LOOP account"),
                            ("admin", "Admin decision"),
                        ],
                        max_length=20,
                    ),
                ),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="access_decisions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="accessrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ["pending", "approved"])),
                fields=("user", "resource"),
                name="access_one_open_request",
            ),
        ),
    ]
