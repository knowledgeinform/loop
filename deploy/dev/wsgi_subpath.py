"""WSGI wrapper that strips the ``/loop`` prefix before Django sees the request.

WHY THIS IS NEEDED

``DJANGO_SUBPATH=/loop`` sets ``FORCE_SCRIPT_NAME``, which makes Django *generate*
URLs with a ``/loop`` prefix -- but ``loop/urls.py`` mounts its patterns at the
root (``path('', include('catalog.urls'))``). So Django assumes something in front
of it strips the prefix and passes the remainder as ``PATH_INFO``. In production
a reverse proxy does exactly that.

A Cloudflare tunnel cannot rewrite paths: an ingress rule matching ``^/loop``
forwards the URL untouched. Django then receives ``PATH_INFO=/loop/accounts/login/``,
matches nothing, and redirects to the login page with the prefix applied *again* --
producing ``/loop/loop/accounts/login/?next=/loop/loop/...`` and an infinite
redirect chain that grows one prefix per hop.

This shim restores the assumption Django is written against, keeping dev's URL
shape identical to production instead of papering over it by serving LOOP at the
hostname root.
"""

import os

from loop.wsgi import application as _django_app

PREFIX = os.environ.get("DEV_STRIP_PREFIX", "/loop").rstrip("/")


def application(environ, start_response):
    if PREFIX:
        path = environ.get("PATH_INFO", "")
        if path == PREFIX or path.startswith(PREFIX + "/"):
            # SCRIPT_NAME is what a real proxy would set; PATH_INFO carries the
            # remainder. "/loop" alone becomes "/" so the index view still matches.
            environ["SCRIPT_NAME"] = PREFIX
            environ["PATH_INFO"] = path[len(PREFIX):] or "/"
    return _django_app(environ, start_response)
