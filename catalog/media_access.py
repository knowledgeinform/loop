"""Affiliation-gated serving of uploaded media files.

Apache proxies ``/loop/media/`` to Django but Django never serves MEDIA_URL
with ``DEBUG=False``, so every stored ``raw_data_link`` has always returned 404.
The obvious fix, an Apache ``Alias`` next to the one for static files, would
publish every uploaded pattern to anyone who guessed a URL. LOOP's data is
restricted to the collaboration, and Apache cannot see LOOP accounts or
affiliations, so the gate has to live here.

Serving through Django keeps the existing links working unchanged: the paths
already stored on trials resolve to this view, so no records need rewriting.

Layout this understands (see ``xrd_store``)::

    MEDIA_ROOT/xrd/<material_auid>/<recipe_hash>/<trial_id>/<file>
    MEDIA_ROOT/xrd_data/<material_auid>/<trial_id>/<file>      (legacy)

Anything whose owning trial cannot be identified is refused. A media tree is
not a place to be permissive: an unrecognised layout is far more likely to be a
path we forgot to reason about than a file that ought to be public.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Optional, Tuple

from django.conf import settings
from django.http import FileResponse, Http404
from django.contrib.auth.decorators import login_required

from .documents import find_embedded_trial, get_recipe
from .permissions import is_visible_to_user

logger = logging.getLogger(__name__)

# Subdirectories of MEDIA_ROOT this view will consider serving at all.
# Everything else under MEDIA_ROOT (upload manifests, batch archives) is
# internal bookkeeping with no per-record visibility to check against.
_SERVABLE_ROOTS = ("xrd", "xrd_data")


def _resolve_within_media_root(relative_path: str) -> Optional[Path]:
    """Resolve ``relative_path`` under MEDIA_ROOT, or None if it escapes.

    ``..`` segments and symlinks are both handled by resolving the candidate
    and confirming MEDIA_ROOT is one of its parents. Rejecting on the raw
    string instead would miss a symlink pointing out of the tree.
    """
    media_root = Path(settings.MEDIA_ROOT).resolve()
    candidate = (media_root / relative_path).resolve()
    if candidate != media_root and media_root not in candidate.parents:
        return None
    return candidate


def _owning_trial(relative_path: str) -> Optional[Tuple[str, str]]:
    """Map a media path to the ``(recipe_auid, trial_id)`` that owns it.

    Returns None when the path does not match a known layout, which the caller
    must treat as "refuse", not "allow".
    """
    parts = Path(relative_path).parts
    if len(parts) < 2:
        return None
    root = parts[0]

    if root == "xrd" and len(parts) >= 4:
        # xrd/<material>/<recipe>/<trial>/...
        material, recipe, trial_id = parts[1], parts[2], parts[3]
        return f"{material}:{recipe}", trial_id

    if root == "xrd_data" and len(parts) >= 3:
        # Legacy: xrd_data/<material>/<trial>/... has no recipe segment, so the
        # owning recipe has to be found by searching the material's recipes.
        material, trial_id = parts[1], parts[2]
        return material, trial_id

    return None


def _is_visible(recipe_or_material: str, trial_id: str, user) -> bool:
    from .views import _user_affiliations  # local import avoids a cycle

    affiliations = _user_affiliations(user)
    if getattr(user, "is_superuser", False):
        return True

    recipe = get_recipe(recipe_or_material)
    if recipe is None:
        # Legacy layout: the path named a material, not a recipe. Any recipe of
        # that material carrying this trial and visible to the user grants it.
        from .documents import get_recipes_for_material

        try:
            candidates = get_recipes_for_material(recipe_or_material) or []
        except Exception:
            candidates = []
        for candidate in candidates:
            trial = find_embedded_trial(candidate, trial_id)
            if trial is not None and is_visible_to_user(
                getattr(trial, "visibility_affiliations", None), affiliations
            ):
                return True
        return False

    trial = find_embedded_trial(recipe, trial_id)
    if trial is None:
        return False
    return is_visible_to_user(
        getattr(trial, "visibility_affiliations", None), affiliations
    )


@login_required
def protected_media(request, relative_path: str):
    """Serve an uploaded media file if the signed-in user may see its trial.

    Refusals are 404 rather than 403 throughout. A 403 would confirm that a
    given material, recipe and trial exist to someone outside the affiliation,
    and the AUIDs are content-derived, so that is a real disclosure.
    """
    path = _resolve_within_media_root(relative_path)
    if path is None:
        logger.warning("Rejected media path escaping MEDIA_ROOT: %r", relative_path)
        raise Http404

    if not path.is_file():
        raise Http404

    parts = Path(relative_path).parts
    if not parts or parts[0] not in _SERVABLE_ROOTS:
        raise Http404

    owner = _owning_trial(relative_path)
    if owner is None:
        raise Http404

    if not _is_visible(owner[0], owner[1], request.user):
        raise Http404

    content_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path.open("rb"),
        content_type=content_type or "application/octet-stream",
    )
