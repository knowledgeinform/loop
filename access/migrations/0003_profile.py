from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("access", "0002_backfill_verified_emails"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Profile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("institution", models.CharField(max_length=200)),
                ("ror_id", models.CharField(blank=True, max_length=40)),
                ("ror_type", models.CharField(blank=True, max_length=40)),
                ("country", models.CharField(max_length=2)),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("undergraduate", "Undergraduate student"),
                            ("graduate", "Graduate student"),
                            ("postdoc", "Postdoctoral researcher"),
                            ("faculty", "Faculty"),
                            ("scientist", "Research scientist or engineer"),
                            ("staff", "Staff"),
                            ("other", "Other"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "sector",
                    models.CharField(
                        choices=[
                            ("academia", "University or college"),
                            ("lab", "National laboratory or research institute"),
                            ("industry", "Industry"),
                            ("government", "Government"),
                            ("nonprofit", "Nonprofit"),
                            ("other", "Other"),
                        ],
                        max_length=20,
                    ),
                ),
                ("research_areas", models.JSONField(default=list)),
                ("intended_use", models.TextField(max_length=1000)),
                ("orcid", models.CharField(blank=True, max_length=19)),
                (
                    "heard_from",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("", "(no answer)"),
                            ("paper", "A paper"),
                            ("colleague", "A colleague or advisor"),
                            ("talk", "A talk or conference"),
                            ("course", "A course"),
                            ("search", "A web search"),
                            ("social", "Social media"),
                            ("other", "Other"),
                        ],
                        max_length=20,
                    ),
                ),
                ("contact_ok", models.BooleanField(default=False)),
                ("completed_at", models.DateTimeField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
