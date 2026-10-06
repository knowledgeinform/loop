"""Pages for requesting CHAOS access and reading its terms, and the profile page."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache

from access import policy
from access.forms import ChaosAccessRequestForm, ChaosTermsForm, ProfileForm
from access.models import AccessRequest, Profile


def _terms_context():
    version = policy.chaos_terms_version()
    return {
        "terms_version": version,
        "terms_template": "access/terms/chaos.html",
    }


@never_cache
@login_required
def chaos_access(request):
    """Request CHAOS access, or accept a new terms version, or see the status."""
    user = request.user
    policy.reconcile(user)
    policy.refresh_pending(user)
    open_request = policy.open_chaos_request(user)
    in_group = policy.has_chaos_group(user)
    terms_current = policy.accepted_current_chaos_terms(user)

    # State shown on the page:
    #   granted       group (or staff) and current terms
    #   terms_needed  group, but the terms changed since acceptance
    #   pending       request waiting for an admin
    #   request       nothing yet (or an old denied/revoked request)
    if policy.has_chaos_access(user):
        state = "granted"
    elif in_group and not terms_current:
        state = "terms_needed"
    elif open_request is not None and open_request.status == AccessRequest.PENDING:
        state = "pending"
    else:
        state = "request"

    form = None
    if state == "request":
        # The profile already holds the institution and the intended use.
        profile = Profile.objects.filter(user=user).first()
        initial = {"affiliation": profile.institution, "purpose": profile.intended_use} if profile else None
        form = ChaosAccessRequestForm(request.POST or None, initial=initial)
        if request.method == "POST" and form.is_valid():
            access_request = policy.request_chaos_access(
                user,
                affiliation=form.cleaned_data["affiliation"],
                purpose=form.cleaned_data["purpose"],
                request=request,
            )
            if access_request.status == AccessRequest.APPROVED:
                messages.success(request, "CHAOS access granted.")
            else:
                messages.info(request, "Request received. You will get an email when it is decided.")
            return redirect("chaos_access")
    elif state == "terms_needed":
        form = ChaosTermsForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            policy.record_chaos_terms(user, request)
            messages.success(request, "Terms accepted.")
            return redirect("chaos_access")

    context = {
        "state": state,
        "form": form,
        "open_request": open_request,
        "automatic_route": policy.automatic_route(user) if state == "request" else None,
        "prior_refusal": policy.prior_refusal(user) if state == "request" else False,
        "hide_sidebar": True,
    }
    context.update(_terms_context())
    return render(request, "access/chaos_access.html", context)


def chaos_terms(request):
    """The current CHAOS terms, readable without an account."""
    context = {"hide_sidebar": True}
    context.update(_terms_context())
    return render(request, "access/chaos_terms.html", context)


@never_cache
@login_required
def profile(request):
    """The profile every account fills in once, with the usage notice.

    New accounts see it at their first sign-in; existing accounts at their next
    request (ProfileRequiredMiddleware). It stays available to update later.
    """
    user = request.user
    instance = Profile.objects.filter(user=user).first()
    first_time = not policy.profile_complete(user)
    form = ProfileForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.user = user
        if saved.completed_at is None:
            saved.completed_at = timezone.now()
        saved.save()
        policy.record_usage_notice(user, request)
        messages.success(request, "Profile saved.")
        target = request.POST.get("next") or request.GET.get("next") or ""
        if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            target = reverse("index")
        return redirect(target)
    context = {
        "form": form,
        "first_time": first_time,
        "next": request.GET.get("next", ""),
        "usage_notice_version": policy.usage_notice_version(),
        "usage_contact": getattr(settings, "USAGE_CONTACT_EMAIL", ""),
        "hide_sidebar": True,
    }
    return render(request, "access/profile.html", context)
