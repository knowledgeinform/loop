from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.contrib import messages

import re

DEFAULT_EXEMPT = (
    r"^$",                         # /
    r"^index/?$",
    r"^catalog/?$",
    r"^accounts/",                 # auth pages
    r"^signup/pending/?$",
    r"^signup/complete/?$",
    r"^awaiting-approval/?$",
    r"^admin/",                    # ⬅ allow the whole admin tree
    r"^static/",
    r"^media/",
)

class ApprovedGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        patterns = getattr(settings, "ACCESS_EXEMPT_URLS", DEFAULT_EXEMPT)
        login_path = settings.LOGIN_URL.lstrip("/")
        self.exempt = [re.compile(p) for p in patterns] + [re.compile(f"^{re.escape(login_path)}")]

        self.approved_group = getattr(settings, "APPROVED_GROUP_NAME", "approved")
        self.superuser_bypass = getattr(settings, "APPROVED_BYPASS_SUPERUSERS", True)

    def __call__(self, request):
        path = request.path_info.lstrip("/")

        # Public or auth routes pass through
        if any(p.match(path) for p in self.exempt):
            return self.get_response(request)

        user = request.user

        # Not logged in → send to login with ?next=
        if not user.is_authenticated:
            return redirect_to_login(next=request.get_full_path())

        # let staff/superusers bypass the gate
        if self.superuser_bypass and (user.is_superuser or user.is_staff):
            return self.get_response(request)

        # Must be in the "approved" group
        if not user.groups.filter(name=self.approved_group).exists():
            return redirect(getattr(settings, "AWAITING_APPROVAL_URL_NAME", "awaiting_approval"))

        # All good
        return self.get_response(request)