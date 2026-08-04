"""
URL configuration for Loop project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views

from django.urls import path, include
from django.views.generic import RedirectView, TemplateView

from catalog import views as catalog_views
from catalog import discovery_views
from catalog.views import SignUpView, SignupPendingView, SignupCompleteView, activate
from catalog.forms import LoopAuthenticationForm


def _static_patterns_for(url, document_root):
    patterns = list(static(url, document_root=document_root))

    script_prefix = getattr(settings, "FORCE_SCRIPT_NAME", "") or ""
    if script_prefix and url.startswith(f"{script_prefix}/"):
        stripped_url = url[len(script_prefix):]
        patterns += static(stripped_url, document_root=document_root)

    return patterns


# See DEV_LOCKDOWN in settings.py. Read once here so the whole file agrees, and
# so a test can reload this module to exercise both shapes.
LOCKDOWN = getattr(settings, "DEV_LOCKDOWN", False)

urlpatterns = [
    # Public discovery documents for humans, clients, and LLM agents.
    path('llms.txt', discovery_views.llms_txt, name='llms-txt'),
    path('docs.url', discovery_views.docs_url, name='docs-url'),
    path('developers/api.md', discovery_views.api_markdown, name='api-markdown'),
    path('developers/agent.md', discovery_views.agent_markdown, name='agent-markdown'),
]

if not LOCKDOWN:
    urlpatterns += [path('admin/', admin.site.urls)]

urlpatterns += [
    # Versioned machine API (DRF); auth via catalog.api.authentication.
    path('api/v1/', include('catalog.api.urls')),
    # Older docs and bookmarks used /catalog/; app routes now live at the site root.
    path('catalog/', RedirectView.as_view(url='/', permanent=False)),
    path('', include('catalog.urls')),
]

urlpatterns += _static_patterns_for(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

# add django auth urls (for login, logout, password management)
urlpatterns += [
    path(
        'accounts/login/',
        auth_views.LoginView.as_view(authentication_form=LoopAuthenticationForm),
        name='login'
    ),
    path('accounts/', include('django.contrib.auth.urls')),
]

# Self-registration: the form plus the pages and the activation link it hands
# out. Under lockdown the whole flow goes, not just the entry point, since the
# later steps only exist to finish a signup that can no longer be started.
if not LOCKDOWN:
    urlpatterns += [
        path("accounts/signup/", SignUpView.as_view(), name="signup"),
        path("accounts/signup/pending/", SignupPendingView.as_view(), name="signup_pending"),
        path("accounts/signup/complete/", SignupCompleteView.as_view(), name="signup_complete"),
        path("accounts/activate/<uidb64>/<token>/", activate, name="activate"),
    ]

urlpatterns += [
    path("accounts/awaiting-approval/", TemplateView.as_view(template_name="registration/awaiting_approval.html"), name="awaiting_approval"),
]

# in debug only - don't want to serve csv's directly
if settings.DEBUG:
    urlpatterns += _static_patterns_for(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler400 = 'catalog.views.custom_bad_request'
handler404 = 'catalog.views.custom_page_not_found'
# 500s under /api/v1/ answer problem+json like every other API error; DRF's
# exception hook only converts APIException subclasses, so an ordinary bug in a
# view would otherwise render the HTML error page to a JSON client.
handler500 = 'catalog.views.custom_server_error'
