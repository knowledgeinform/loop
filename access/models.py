"""Records behind site access: verified addresses, terms, access requests.

LOOP's own approval stays where it was (the ``Approved`` group). These models
add what CHAOS access needs: proof that an address was verified, a record of
which terms each account accepted, and one request per account with how it
was decided.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q


class VerifiedEmail(models.Model):
    """An address the account proved it controls through the activation link."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="verified_emails"
    )
    email = models.EmailField()
    verified_at = models.DateTimeField()

    class Meta:
        ordering = ["-verified_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "email"], name="access_verified_email_once"),
        ]

    def __str__(self):
        return f"{self.user} <{self.email}>"


class TermsAcceptance(models.Model):
    """One accepted version of one terms document."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="terms_acceptances"
    )
    document = models.CharField(max_length=20)
    version = models.CharField(max_length=40)
    accepted_at = models.DateTimeField(auto_now_add=True)
    # A record for the file, not a security control: behind Cloudflare the
    # visitor address arrives in CF-Connecting-IP.
    ip = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-accepted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "document", "version"], name="access_terms_once_per_version"
            ),
        ]

    def __str__(self):
        return f"{self.user} accepted {self.document} {self.version}"


class AccessRequest(models.Model):
    """A request for one resource, and how it was decided."""

    RESOURCE_CHAOS = "chaos"
    RESOURCE_CHOICES = [(RESOURCE_CHAOS, "CHAOS")]

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "revoked"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (APPROVED, "Approved"),
        (DENIED, "Denied"),
        (REVOKED, "Revoked"),
    ]

    VIA_VERIFIED_EMAIL = "verified_email"
    VIA_LOOP_APPROVED = "loop_approved"
    VIA_ADMIN = "admin"
    VIA_CHOICES = [
        (VIA_VERIFIED_EMAIL, "Verified address with an allowed ending"),
        (VIA_LOOP_APPROVED, "Approved LOOP account"),
        (VIA_ADMIN, "Admin decision"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="access_requests"
    )
    resource = models.CharField(max_length=20, choices=RESOURCE_CHOICES, default=RESOURCE_CHAOS)
    affiliation = models.CharField(max_length=200)
    purpose = models.TextField(max_length=2000)
    email_at_request = models.EmailField()
    terms_version = models.CharField(max_length=40)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    decided_via = models.CharField(max_length=20, choices=VIA_CHOICES, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="access_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one live request (pending or approved) per account and
            # resource; denied and revoked ones stay as history.
            models.UniqueConstraint(
                fields=["user", "resource"],
                condition=Q(status__in=["pending", "approved"]),
                name="access_one_open_request",
            ),
        ]

    def __str__(self):
        return f"{self.user} {self.resource} {self.status}"


class Profile(models.Model):
    """Who an account belongs to, asked once of every account.

    Why: the S4E Laboratory reports who uses its tools (institutions, sectors,
    countries, career stages, research areas, what the data is used for) in
    proposals and reports to funders, and before 2026-09 LOOP asked for none of
    it. Decided by Corey Oses, 2026-09-29: every account, new and existing,
    fills this in before it can use LOOP or CHAOS again (ProfileRequired in
    access/middleware.py). The same page shows the notice that use of the
    tools is recorded with the account (access/usage.py); accepting it is a
    TermsAcceptance with document "usage".
    """

    ROLE_CHOICES = [
        ("undergraduate", "Undergraduate student"),
        ("graduate", "Graduate student"),
        ("postdoc", "Postdoctoral researcher"),
        ("faculty", "Faculty"),
        ("scientist", "Research scientist or engineer"),
        ("staff", "Staff"),
        ("other", "Other"),
    ]
    SECTOR_CHOICES = [
        ("academia", "University or college"),
        ("lab", "National laboratory or research institute"),
        ("industry", "Industry"),
        ("government", "Government"),
        ("nonprofit", "Nonprofit"),
        ("other", "Other"),
    ]
    AREA_CHOICES = [
        ("energy_storage", "Batteries and energy storage"),
        ("catalysis", "Catalysis"),
        ("electrochemistry", "Fuel cells and electrolysis"),
        ("thermal", "Thermoelectrics and thermal materials"),
        ("structural", "Structural and high-temperature materials"),
        ("functional", "Electronic, magnetic and optical materials"),
        ("high_entropy", "High-entropy materials"),
        ("ml", "Machine learning and data science"),
        ("computation", "Computational methods"),
        ("synthesis", "Synthesis and experiments"),
        ("teaching", "Teaching"),
        ("other", "Other"),
    ]
    HEARD_CHOICES = [
        ("", "(no answer)"),
        ("paper", "A paper"),
        ("colleague", "A colleague or advisor"),
        ("talk", "A talk or conference"),
        ("course", "A course"),
        ("search", "A web search"),
        ("social", "Social media"),
        ("other", "Other"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    institution = models.CharField(max_length=200)
    # The Research Organization Registry id when the institution was picked
    # from ROR (https://ror.org), with the type ROR gives it (education,
    # company, government, facility, ...). Empty when typed by hand.
    ror_id = models.CharField(max_length=40, blank=True)
    ror_type = models.CharField(max_length=40, blank=True)
    country = models.CharField(max_length=2)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    sector = models.CharField(max_length=20, choices=SECTOR_CHOICES)
    research_areas = models.JSONField(default=list)
    intended_use = models.TextField(max_length=1000)
    orcid = models.CharField(max_length=19, blank=True)
    heard_from = models.CharField(max_length=20, choices=HEARD_CHOICES, blank=True)
    contact_ok = models.BooleanField(default=False)
    completed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} ({self.institution}, {self.country})"

    # For display (LOOP's Account page): names instead of the stored codes.
    @property
    def country_name(self):
        from .countries import COUNTRIES

        return dict(COUNTRIES).get(self.country, self.country)

    @property
    def research_area_labels(self):
        names = dict(self.AREA_CHOICES)
        return [names.get(code, code) for code in self.research_areas or []]
