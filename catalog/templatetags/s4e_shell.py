"""Shared Entropy for Energy site shell.

The group site (entropy4energy.github.io, served at s4e.ai) renders its
header, navigation, sub-navigation, news sidebar and footer once, as
fragments under https://s4e.ai/partials/. LOOP includes them at request
time so the shell is never copied here and cannot drift:

    {% load s4e_shell %}
    {% s4e_partial "head" %}          (versioned link to shell.css)
    {% s4e_partial "header-loop" %}   (hero with the LOOP product line)
    {% s4e_partial "nav" %}
    {% s4e_partial "subnav" %}
    {% s4e_partial "sidebar" %}
    {% s4e_partial "footer-loop" %}

Fragments are cached for S4E_SHELL_CACHE_SECONDS. When s4e.ai does not
answer, the copy committed under templates/s4e/partials/ is used instead
(and cached briefly so a dead upstream is not polled on every request).
Setting S4E_SHELL_URL to an empty string turns fetching off, which is
what local development wants.

The active tab and product are not in the fragments: <body> carries
data-s4e-tab="tools" data-s4e-product="loop" and shell.css lights them.
"""

import logging
import re
from urllib.request import Request, urlopen

from django import template
from django.conf import settings
from django.core.cache import cache
from django.template.loader import get_template
from django.utils.safestring import mark_safe

logger = logging.getLogger(__name__)
register = template.Library()

_NAME = re.compile(r"^[a-z]+(-[a-z0-9]+)?$")
FETCH_TIMEOUT = 3.0
FAILURE_CACHE_SECONDS = 60


def _fallback(name: str) -> str:
    try:
        return get_template(f"s4e/partials/{name}.html").render({})
    except template.TemplateDoesNotExist:
        logger.warning("s4e_partial: no fallback for %r", name)
        return ""


def _fetch(name: str) -> str | None:
    base = getattr(settings, "S4E_SHELL_URL", "")
    if not base:
        return None
    url = f"{base.rstrip('/')}/partials/{name}.html"
    try:
        request = Request(url, headers={"User-Agent": "LOOP s4e_shell"})
        with urlopen(request, timeout=FETCH_TIMEOUT) as response:
            if response.status != 200:
                return None
            return response.read().decode("utf-8")
    except Exception as exc:  # network, DNS, HTTP: all mean "use the fallback"
        logger.warning("s4e_partial: %s: %s", url, exc)
        return None


@register.simple_tag
def s4e_partial(name: str) -> str:
    if not _NAME.match(name):
        raise template.TemplateSyntaxError(f"s4e_partial: bad fragment name {name!r}")
    key = f"s4e_shell:{name}"
    html = cache.get(key)
    if html is None:
        html = _fetch(name)
        if html is not None:
            cache.set(key, html, getattr(settings, "S4E_SHELL_CACHE_SECONDS", 600))
        else:
            html = _fallback(name)
            cache.set(key, html, FAILURE_CACHE_SECONDS)
    return mark_safe(html)
