from django.contrib import admin, messages

from access import policy
from access.models import AccessRequest, Profile, TermsAcceptance, VerifiedEmail


def _in_access_admin(request):
    """Whether the request is on one of this app's own admin pages.

    Deleting access records there is refused; deleting a user elsewhere in the
    admin still removes that user's records along with the account.
    """
    match = getattr(request, "resolver_match", None)
    return bool(match and (match.url_name or "").startswith("access_"))


@admin.register(AccessRequest)
class AccessRequestAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "resource",
        "status",
        "decided_via",
        "email_at_request",
        "affiliation",
        "created_at",
        "decided_at",
    )
    list_filter = ("resource", "status", "decided_via")
    search_fields = ("user__username", "email_at_request", "affiliation", "purpose")
    readonly_fields = (
        "user",
        "resource",
        "affiliation",
        "purpose",
        "email_at_request",
        "terms_version",
        "status",
        "decided_via",
        "decided_by",
        "decided_at",
        "created_at",
    )
    fields = readonly_fields + ("note",)
    actions = ["approve_selected", "deny_selected", "revoke_selected"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # Rows are the history the policy relies on (a denied or revoked
        # account is never re-granted automatically), and deleting an approved
        # row would leave the group in place. Revoke instead.
        return not _in_access_admin(request)

    def _apply(self, request, queryset, status, action, verb):
        done = 0
        for access_request in queryset.filter(status=status):
            action(access_request, request)
            done += 1
        skipped = queryset.count() - done
        self.message_user(request, f"{verb} {done} request(s).", messages.SUCCESS)
        if skipped:
            self.message_user(
                request, f"Skipped {skipped} request(s) in another state.", messages.WARNING
            )

    @admin.action(description="Approve selected pending requests")
    def approve_selected(self, request, queryset):
        self._apply(
            request,
            queryset,
            AccessRequest.PENDING,
            lambda ar, req: policy.approve(ar, via=AccessRequest.VIA_ADMIN, by=req.user),
            "Approved",
        )

    @admin.action(description="Deny selected pending requests")
    def deny_selected(self, request, queryset):
        self._apply(
            request,
            queryset,
            AccessRequest.PENDING,
            lambda ar, req: policy.deny(ar, by=req.user),
            "Denied",
        )

    @admin.action(description="Revoke selected approved requests")
    def revoke_selected(self, request, queryset):
        self._apply(
            request,
            queryset,
            AccessRequest.APPROVED,
            lambda ar, req: policy.revoke(ar, by=req.user),
            "Revoked",
        )


@admin.register(TermsAcceptance)
class TermsAcceptanceAdmin(admin.ModelAdmin):
    list_display = ("user", "document", "version", "accepted_at", "ip")
    list_filter = ("document", "version")
    search_fields = ("user__username",)
    readonly_fields = ("user", "document", "version", "accepted_at", "ip", "user_agent")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return not _in_access_admin(request)


@admin.register(VerifiedEmail)
class VerifiedEmailAdmin(admin.ModelAdmin):
    list_display = ("user", "email", "verified_at")
    search_fields = ("user__username", "email")
    readonly_fields = ("user", "email", "verified_at")

    def has_add_permission(self, request):
        return False


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    """Who the accounts are; the numbers behind usage reports."""

    list_display = ("user", "institution", "country", "sector", "role", "ror_type", "completed_at")
    list_filter = ("country", "sector", "role", "ror_type", "contact_ok")
    search_fields = ("user__username", "user__email", "institution", "ror_id", "orcid", "intended_use")
    readonly_fields = ("completed_at", "updated_at")
