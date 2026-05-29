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
from catalog.views import SignUpView, SignupPendingView, SignupCompleteView, activate
from catalog.forms import LoopAuthenticationForm


def _static_patterns_for(url, document_root):
    patterns = list(static(url, document_root=document_root))

    script_prefix = getattr(settings, "FORCE_SCRIPT_NAME", "") or ""
    if script_prefix and url.startswith(f"{script_prefix}/"):
        stripped_url = url[len(script_prefix):]
        patterns += static(stripped_url, document_root=document_root)

    return patterns


urlpatterns = [
    path('admin/', admin.site.urls),
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

urlpatterns += [
    path("accounts/signup/", SignUpView.as_view(), name="signup"),
    path("accounts/signup/pending/", SignupPendingView.as_view(), name="signup_pending"),
    path("accounts/signup/complete/", SignupCompleteView.as_view(), name="signup_complete"),
    path("accounts/activate/<uidb64>/<token>/", activate, name="activate"),
    path("accounts/awaiting-approval/", TemplateView.as_view(template_name="registration/awaiting_approval.html"), name="awaiting_approval"),
]

# in debug only - don't want to serve csv's directly
if settings.DEBUG:
    urlpatterns += _static_patterns_for(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler400 = 'catalog.views.custom_bad_request'
