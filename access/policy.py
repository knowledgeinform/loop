"""Who may pass which curtain on s4e.ai.

LOOP: the ``Approved`` group and an affiliation set by an admin
(``has_loop_access``). ``ApprovedGateMiddleware`` has an exemption for
``/access/``, whose views check the login themselves.

CHAOS: the CHAOS group (``CHAOS_GROUP_NAME``) plus acceptance of the current
CHAOS terms (``CHAOS_TERMS_VERSION``). A request is granted at once when the
account is LOOP-approved, or when its current address was verified through the
activation link and the domain ends in one of ``CHAOS_AUTO_EMAIL_SUFFIXES``
(".edu" by default). Every other request waits for an admin, and so does
any request from an account whose earlier CHAOS request was denied or whose
access was revoked: those decisions are not undone automatically.

Staff and superusers pass both curtains when ``APPROVED_BYPASS_SUPERUSERS`` is
on, as they already do for LOOP.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from access.models import AccessRequest, Profile, TermsAcceptance, VerifiedEmail

logger = logging.getLogger(__name__)

CHAOS = AccessRequest.RESOURCE_CHAOS
CHAOS_TERMS_DOCUMENT = "chaos"
CHAOS_KEY_SCOPE = "chaos:read"


# --- settings -----------------------------------------------------------------

def loop_group_name():
    return getattr(settings, "APPROVED_GROUP_NAME", "Approved")


def chaos_group_name():
    return getattr(settings, "CHAOS_GROUP_NAME", "CHAOS")


def chaos_terms_version():
    return getattr(settings, "CHAOS_TERMS_VERSION", "draft-2026-09")


def auto_email_suffixes():
    return list(getattr(settings, "CHAOS_AUTO_EMAIL_SUFFIXES", [".edu"]))


def admin_addresses():
    """Where pending requests are announced: the setting, else active superusers."""
    configured = [a for a in getattr(settings, "ACCESS_ADMIN_EMAILS", []) if a]
    if configured:
        return configured
    return list(
        get_user_model()
        .objects.filter(is_superuser=True, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )


# --- checks -------------------------------------------------------------------

def _signed_in(user):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active)


def _bypass(user):
    return bool(
        getattr(settings, "APPROVED_BYPASS_SUPERUSERS", True)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def _in_group(user, name):
    return user.groups.filter(name=name).exists()


def loop_affiliations(user):
    """The partner affiliations set on this account, with no default.

    Empty when none is set. Sign-up no longer asks for one: an admin sets it
    when approving the account (LOOP's Users page, under Data management).
    The record filters (``catalog.documents.get_user_affiliations``) also
    treat none as none, never as S4E.

    The answer is kept on the user object, which lives for one request, so
    the gate, the API permission and the views ask MongoDB once. If MongoDB
    cannot be read, the account counts as having none (LOOP then waits;
    /api/v1/access/ still answers, so CHAOS is not affected).
    """
    if not _signed_in(user):
        return []
    cached = getattr(user, "_loop_affiliations", None)
    if cached is not None:
        return list(cached)
    from catalog.documents import AFFILIATION_VALUES, UserAffiliation

    try:
        doc = UserAffiliation.objects(user_id=user.id).only("affiliations").first()
    except Exception:
        logger.exception("LOOP affiliations of user %s could not be read", user.id)
        return []
    values = [a for a in (doc.affiliations or []) if a in AFFILIATION_VALUES] if doc else []
    try:
        user._loop_affiliations = tuple(values)
    except AttributeError:
        pass
    return values


def has_loop_access(user):
    """LOOP-approved: in the Approved group and with an affiliation set.

    An approved account without an affiliation waits, as an unapproved one
    does; the record filters, a second guard, would show it nothing. Staff
    and superusers pass when APPROVED_BYPASS_SUPERUSERS is on, as before
    (they still see only what their affiliation allows, except where a view
    gives superusers everything). Every LOOP approval check goes through here: the gate
    middleware, the API permission, the views and /api/v1/access/.
    """
    if not _signed_in(user):
        return False
    if _bypass(user):
        return True
    return _in_group(user, loop_group_name()) and bool(loop_affiliations(user))


def accepted_current_chaos_terms(user):
    if not _signed_in(user):
        return False
    return TermsAcceptance.objects.filter(
        user=user, document=CHAOS_TERMS_DOCUMENT, version=chaos_terms_version()
    ).exists()


def has_chaos_group(user):
    return _signed_in(user) and _in_group(user, chaos_group_name())


def has_chaos_access(user):
    if not _signed_in(user):
        return False
    if _bypass(user):
        return True
    return has_chaos_group(user) and accepted_current_chaos_terms(user)


# --- profile and usage notice ---------------------------------------------------
#
# Every account fills in a profile (access.models.Profile) and accepts the
# notice that use of the tools is recorded (document "usage"), once, before it
# can use LOOP or CHAOS (ProfileRequired in access/middleware.py). A new
# notice version asks again. PROFILE_REQUIRED=0 switches the requirement off
# (the profile page still works), for example if the page itself breaks.

USAGE_DOCUMENT = "usage"


def profile_required():
    return bool(getattr(settings, "PROFILE_REQUIRED", True))


def usage_notice_version():
    return getattr(settings, "USAGE_NOTICE_VERSION", "2026-09")


def profile_complete(user):
    if not _signed_in(user):
        return False
    return (
        Profile.objects.filter(user=user).exists()
        and TermsAcceptance.objects.filter(
            user=user, document=USAGE_DOCUMENT, version=usage_notice_version()
        ).exists()
    )


def profile_ok(user):
    """Whether the profile requirement lets this account through."""
    return not profile_required() or profile_complete(user)


def record_usage_notice(user, request=None):
    ip = user_agent = ""
    if request is not None:
        meta = request.META
        ip = (
            meta.get("HTTP_CF_CONNECTING_IP")
            or meta.get("HTTP_X_REAL_IP")
            or meta.get("REMOTE_ADDR")
            or ""
        )[:64]
        user_agent = meta.get("HTTP_USER_AGENT", "")[:300]
    obj, _ = TermsAcceptance.objects.get_or_create(
        user=user,
        document=USAGE_DOCUMENT,
        version=usage_notice_version(),
        defaults={"ip": ip, "user_agent": user_agent},
    )
    return obj


def email_qualifies(email, suffixes=None):
    """Whether the address's domain ends in an allowed suffix.

    ``jhu.edu`` and ``cs.cmu.edu`` match ".edu"; ``edu.example.com`` does not.
    """
    if not email or email.count("@") != 1:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if "." not in domain:
        return False
    for suffix in auto_email_suffixes() if suffixes is None else suffixes:
        suffix = suffix.strip().lower().strip(".")
        if suffix and domain.endswith("." + suffix):
            return True
    return False


# --- verified addresses ---------------------------------------------------------

def record_verified_email(user):
    """Called when the activation link succeeds: this address is proven."""
    email = (user.email or "").strip().lower()
    if not email:
        return None
    obj, _ = VerifiedEmail.objects.update_or_create(
        user=user, email=email, defaults={"verified_at": timezone.now()}
    )
    return obj


def verified_email(user):
    """The account's current address if it was verified, otherwise None.

    An address changed after verification does not count until it is verified.
    """
    email = (user.email or "").strip().lower()
    if email and VerifiedEmail.objects.filter(user=user, email=email).exists():
        return email
    return None


def prior_refusal(user):
    """Whether an admin has denied a CHAOS request from, or revoked, this account."""
    return AccessRequest.objects.filter(
        user=user,
        resource=CHAOS,
        status__in=[AccessRequest.DENIED, AccessRequest.REVOKED],
    ).exists()


def automatic_route(user):
    """How a CHAOS request from this account is granted without an admin, or None."""
    if prior_refusal(user):
        return None
    if has_loop_access(user):
        return AccessRequest.VIA_LOOP_APPROVED
    email = verified_email(user)
    if email and email_qualifies(email):
        return AccessRequest.VIA_VERIFIED_EMAIL
    return None


# --- requests and decisions -----------------------------------------------------

def open_chaos_request(user):
    return (
        AccessRequest.objects.filter(
            user=user,
            resource=CHAOS,
            status__in=[AccessRequest.PENDING, AccessRequest.APPROVED],
        )
        .order_by("-created_at")
        .first()
    )


def record_chaos_terms(user, request=None):
    ip = ""
    user_agent = ""
    if request is not None:
        meta = request.META
        ip = (
            meta.get("HTTP_CF_CONNECTING_IP")
            or meta.get("HTTP_X_REAL_IP")
            or meta.get("REMOTE_ADDR")
            or ""
        )[:64]
        user_agent = meta.get("HTTP_USER_AGENT", "")[:300]
    obj, _ = TermsAcceptance.objects.get_or_create(
        user=user,
        document=CHAOS_TERMS_DOCUMENT,
        version=chaos_terms_version(),
        defaults={"ip": ip, "user_agent": user_agent},
    )
    return obj


def reconcile(user):
    """Keep the request record in step with the group.

    An approved request whose account no longer has the CHAOS group (removed in
    the user admin, or CHAOS_GROUP_NAME changed) is marked revoked, so the
    record matches what the account can do and a new request goes to an admin.
    """
    approved = AccessRequest.objects.filter(
        user=user, resource=CHAOS, status=AccessRequest.APPROVED
    ).first()
    if approved is not None and not has_chaos_group(user) and not _bypass(user):
        revoke(approved, note="CHAOS group removed outside the access requests", notify=False)


def refresh_pending(user):
    """Grant a pending request whose account has since become eligible."""
    pending = AccessRequest.objects.filter(
        user=user, resource=CHAOS, status=AccessRequest.PENDING
    ).first()
    if pending is None:
        return None
    route = automatic_route(user)
    if route:
        approve(pending, via=route)
    return pending


@transaction.atomic
def request_chaos_access(user, *, affiliation, purpose, request=None):
    """Record the terms and a request; grant at once when a route applies."""
    record_chaos_terms(user, request)
    existing = open_chaos_request(user)
    if existing is not None:
        if existing.status == AccessRequest.PENDING:
            refresh_pending(user)
            existing.refresh_from_db()
        return existing
    try:
        with transaction.atomic():
            access_request = AccessRequest.objects.create(
                user=user,
                resource=CHAOS,
                affiliation=affiliation.strip(),
                purpose=purpose.strip(),
                email_at_request=user.email or "",
                terms_version=chaos_terms_version(),
            )
    except IntegrityError:
        # A second submission raced this one; the first one stands.
        return open_chaos_request(user)
    route = automatic_route(user)
    if route:
        approve(access_request, via=route)
    else:
        transaction.on_commit(lambda: _notify_admins(access_request, request))
    return access_request


def approve(access_request, *, via, by=None, note=""):
    access_request.status = AccessRequest.APPROVED
    access_request.decided_via = via
    access_request.decided_by = by
    access_request.decided_at = timezone.now()
    if note:
        access_request.note = note
    access_request.save()
    group, _ = Group.objects.get_or_create(name=chaos_group_name())
    access_request.user.groups.add(group)
    transaction.on_commit(lambda: _notify_user(access_request))


def deny(access_request, *, by=None, note=""):
    access_request.status = AccessRequest.DENIED
    access_request.decided_via = AccessRequest.VIA_ADMIN
    access_request.decided_by = by
    access_request.decided_at = timezone.now()
    if note:
        access_request.note = note
    access_request.save()
    _remove_chaos_group(access_request.user)
    transaction.on_commit(lambda: _notify_user(access_request))


def revoke(access_request, *, by=None, note="", notify=True):
    access_request.status = AccessRequest.REVOKED
    access_request.decided_via = AccessRequest.VIA_ADMIN
    access_request.decided_by = by
    access_request.decided_at = timezone.now()
    if note:
        access_request.note = note
    access_request.save()
    _remove_chaos_group(access_request.user)
    if notify:
        transaction.on_commit(lambda: _notify_user(access_request))


def _remove_chaos_group(user):
    group = Group.objects.filter(name=chaos_group_name()).first()
    if group is not None:
        user.groups.remove(group)


# --- mail ---------------------------------------------------------------------

def _notify_admins(access_request, request=None):
    recipients = admin_addresses()
    if not recipients:
        return
    link = ""
    try:
        path = reverse("admin:access_accessrequest_change", args=[access_request.pk])
        link = request.build_absolute_uri(path) if request is not None else path
    except NoReverseMatch:
        pass
    user = access_request.user
    body = (
        f"CHAOS access request from {user.get_username()} <{access_request.email_at_request}>\n"
        f"Verified address: {'yes' if verified_email(user) else 'no'}\n"
        f"Affiliation: {access_request.affiliation}\n"
        f"Intended use:\n{access_request.purpose}\n\n"
        f"{link}\n"
    )
    EmailMessage(
        subject=f"CHAOS access request: {user.get_username()}",
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
        reply_to=[access_request.email_at_request] if access_request.email_at_request else None,
    ).send(fail_silently=True)


def _notify_user(access_request):
    address = access_request.user.email
    if not address:
        return
    if access_request.status == AccessRequest.APPROVED:
        subject = "CHAOS access granted"
        body = (
            f"Hi {access_request.user.get_username()},\n\n"
            "Your account can now use the CHAOS database on s4e.ai: the pages, the "
            "API with a personal key (scope chaos:read), and the downloads for your "
            "access level.\n"
        )
    elif access_request.status == AccessRequest.DENIED:
        subject = "CHAOS access request"
        body = (
            f"Hi {access_request.user.get_username()},\n\n"
            "Your request for CHAOS access was not approved. Reply to this message "
            "if you have questions.\n"
        )
    elif access_request.status == AccessRequest.REVOKED:
        subject = "CHAOS access withdrawn"
        body = (
            f"Hi {access_request.user.get_username()},\n\n"
            "Your account's access to CHAOS has been withdrawn. Reply to this message "
            "if you have questions.\n"
        )
    else:
        return
    EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[address],
    ).send(fail_silently=True)
