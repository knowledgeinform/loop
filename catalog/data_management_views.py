"""
Superuser-only MongoDB collection browser: list, raw JSON edit, delete.

Uses bson.json_util for round-trip with ObjectId and dates. Supports the
string-keyed collections (materials, recipes, raw_files) alongside the
ObjectId-keyed ones.
"""

from functools import wraps

from bson import json_util
from bson.errors import InvalidId
from bson.objectid import ObjectId
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods
from mongoengine.connection import get_db

from .documents import (
    AFFILIATION_VALUES,
    DOIMapping,
    Material,
    MLEmbedding,
    Recipe,
    STRUCTURE_FAMILY_VALUES,
    UserAffiliation,
    upsert_user_affiliations,
)
from .raw_db import RAW_DB_ALIAS, RawFile

PAGE_SIZE = 25

# slug -> (Document class, human label)
COLLECTIONS = {
    "materials": (Material, "Materials"),
    "recipes": (Recipe, "Recipes"),
    "ml_embeddings": (MLEmbedding, "ML embeddings"),
    "doi_mappings": (DOIMapping, "DOI mappings"),
    "user_affiliations": (UserAffiliation, "User affiliations"),
    "raw_files": (RawFile, "Raw files (backup DB)"),
}

# Collections whose primary key is a string AUID / hash, not an ObjectId.
_STRING_ID_COLLECTIONS = {"materials", "recipes", "raw_files"}

# Collections that carry material_auid + structure_family and support the
# material-search filter toolbar.
_MATERIAL_FILTER_COLLECTIONS = {"materials", "recipes"}


def superuser_required(view_func):
    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied("Only superusers can access data management.")
        return view_func(request, *args, **kwargs)

    return _wrapped


def _ordered_queryset(model):
    fields = getattr(model, "_fields", {}) or {}
    if "created_at" in fields:
        return model.objects.order_by("-created_at")
    if "uploaded_at" in fields:
        return model.objects.order_by("-uploaded_at")
    if "submitted_at" in fields:
        return model.objects.order_by("-submitted_at")
    return model.objects.order_by("-id")


def _preview(doc):
    """Short label for table rows."""
    mat = getattr(doc, "material_auid", None)
    sf = getattr(doc, "structure_family", None)
    if isinstance(doc, Material):
        return f"{doc.id} · {sf or '?'} · {len(doc.dft_calculations or [])} dft"
    if isinstance(doc, Recipe):
        return (
            f"{doc.id} · {len(doc.trials or [])} trials / "
            f"{len(doc.literature or [])} lit"
        )
    if isinstance(doc, RawFile):
        return f"{doc.id} · {doc.original_filename or '?'} · {mat or '?'}"
    if mat and sf is not None:
        return f"{mat} · {sf}"
    if mat:
        return str(mat)
    if getattr(doc, "doi", None):
        return str(doc.doi)[:80]
    if getattr(doc, "username", None) is not None:
        return f"user_id {getattr(doc, 'user_id', '')} · {doc.username}"
    if getattr(doc, "submitted_by", None):
        return f"{doc.submitted_by} · {getattr(doc, 'status', '')}"
    return str(doc.id)


def _document_as_json(doc):
    raw = doc.to_mongo().to_dict()
    return json_util.dumps(raw, indent=2)


def _parse_object_id(object_id, collection):
    """Return the right primary-key value for the collection.

    ``string`` collections (materials, recipes, raw_files) use the raw AUID
    or hash string. Everything else expects a BSON ObjectId.
    """
    if collection in _STRING_ID_COLLECTIONS:
        if not object_id:
            raise Http404("Missing document id")
        return str(object_id)
    try:
        return ObjectId(object_id)
    except InvalidId as exc:
        raise Http404("Invalid document id") from exc


def _get_db_for(collection):
    if collection == "raw_files":
        return get_db(alias=RAW_DB_ALIAS)
    return get_db()


def _get_model(slug):
    entry = COLLECTIONS.get(slug)
    if not entry:
        return None, None
    return entry[0], entry[1]


def _redirect_dm(request, collection):
    """Return to filtered list when `next` is a safe same-site path."""
    target = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect("data_management_collection", collection=collection)


def _filter_material_queryset(qs, collection, request):
    sf = (request.GET.get("structure_family") or "").strip().lower()
    q = (request.GET.get("q") or "").strip()
    if sf and sf in STRUCTURE_FAMILY_VALUES:
        qs = qs.filter(structure_family=sf)
    else:
        sf = ""
    if q:
        if collection == "materials":
            qs = qs.filter(id__icontains=q)
        else:
            qs = qs.filter(material_auid__icontains=q)
    return qs, sf, q


def _collection_query_string(request, page=None):
    """Rebuild GET query for pagination and links (preserves filters)."""
    q = request.GET.copy()
    if page is not None:
        q["page"] = str(page)
    return q.urlencode()


@superuser_required
@require_http_methods(["GET"])
def data_management_index(request):
    User = get_user_model()
    try:
        user_count = User.objects.count()
    except Exception:
        user_count = "—"

    rows = []
    for slug, (model, label) in COLLECTIONS.items():
        try:
            count = model.objects.count()
        except Exception:
            count = "—"
        rows.append({"slug": slug, "label": label, "count": count})
    return render(
        request,
        "catalog/data_management/index.html",
        {"collections": rows, "user_count": user_count},
    )


@superuser_required
@require_http_methods(["GET"])
def data_management_collection(request, collection):
    model, label = _get_model(collection)
    if model is None:
        raise Http404("Unknown collection")

    qs = _ordered_queryset(model)
    filter_structure_family = ""
    filter_q = ""
    if collection in _MATERIAL_FILTER_COLLECTIONS:
        qs, filter_structure_family, filter_q = _filter_material_queryset(qs, collection, request)

    paginator = Paginator(qs, PAGE_SIZE)
    page = request.GET.get("page") or 1
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    items = []
    for doc in page_obj.object_list:
        items.append(
            {
                "id": str(doc.id),
                "preview": _preview(doc),
            }
        )

    ctx = {
        "collection": collection,
        "label": label,
        "items": items,
        "page_obj": page_obj,
        "is_compositions": collection in _MATERIAL_FILTER_COLLECTIONS,
        "structure_families": sorted(STRUCTURE_FAMILY_VALUES),
        "filter_structure_family": filter_structure_family,
        "filter_q": filter_q,
        "query_string": _collection_query_string(request),
        "query_string_page1": _collection_query_string(request, page=1),
    }
    if page_obj.has_previous():
        ctx["qs_prev"] = _collection_query_string(request, page=page_obj.previous_page_number())
    if page_obj.has_next():
        ctx["qs_next"] = _collection_query_string(request, page=page_obj.next_page_number())

    return render(request, "catalog/data_management/collection.html", ctx)


@superuser_required
@require_http_methods(["GET", "POST"])
def data_management_edit(request, collection, object_id):
    model, label = _get_model(collection)
    if model is None:
        raise Http404("Unknown collection")

    oid = _parse_object_id(object_id, collection)
    doc = model.objects(pk=oid).first()
    if doc is None:
        raise Http404("Document not found")

    if request.method == "POST":
        raw_json = (request.POST.get("payload") or "").strip()
        if not raw_json:
            return render(
                request,
                "catalog/data_management/edit.html",
                {
                    "collection": collection,
                    "label": label,
                    "object_id": object_id,
                    "payload": raw_json,
                    "error": "Payload is empty.",
                    "next_url": (request.POST.get("next") or request.GET.get("next") or "").strip(),
                },
                status=400,
            )
        try:
            data = json_util.loads(raw_json)
        except Exception as e:
            return render(
                request,
                "catalog/data_management/edit.html",
                {
                    "collection": collection,
                    "label": label,
                    "object_id": object_id,
                    "payload": raw_json,
                    "error": f"Invalid JSON: {e}",
                    "next_url": (request.POST.get("next") or request.GET.get("next") or "").strip(),
                },
                status=400,
            )

        if "_id" in data:
            existing = data["_id"]
            if isinstance(oid, ObjectId):
                mismatch = (
                    isinstance(existing, ObjectId) and existing != oid
                ) or (not isinstance(existing, ObjectId) and str(existing) != str(oid))
            else:
                mismatch = str(existing) != str(oid)
            if mismatch:
                return render(
                    request,
                    "catalog/data_management/edit.html",
                    {
                        "collection": collection,
                        "label": label,
                        "object_id": object_id,
                        "payload": raw_json,
                        "error": "_id in JSON does not match this document.",
                        "next_url": (request.POST.get("next") or request.GET.get("next") or "").strip(),
                    },
                    status=400,
                )
        data["_id"] = oid

        try:
            coll = _get_db_for(collection)[model._meta["collection"]]
            coll.replace_one({"_id": oid}, data)
        except Exception as e:
            return render(
                request,
                "catalog/data_management/edit.html",
                {
                    "collection": collection,
                    "label": label,
                    "object_id": object_id,
                    "payload": raw_json,
                    "error": f"Save failed: {e}",
                    "next_url": (request.POST.get("next") or request.GET.get("next") or "").strip(),
                },
                status=400,
            )

        return _redirect_dm(request, collection)

    next_url = (request.GET.get("next") or "").strip()
    return render(
        request,
        "catalog/data_management/edit.html",
        {
            "collection": collection,
            "label": label,
            "object_id": object_id,
            "payload": _document_as_json(doc),
            "error": None,
            "next_url": next_url,
        },
    )


@superuser_required
@require_http_methods(["POST"])
def data_management_delete(request, collection, object_id):
    model, _ = _get_model(collection)
    if model is None:
        raise Http404("Unknown collection")

    oid = _parse_object_id(object_id, collection)
    doc = model.objects(pk=oid).first()
    if doc is None:
        raise Http404("Document not found")

    doc.delete()
    return _redirect_dm(request, collection)


# ---------------------------------------------------------------------------
# User management: Django SQLite users joined with MongoDB UserAffiliation
# ---------------------------------------------------------------------------

@superuser_required
@require_http_methods(["GET"])
def data_management_users(request):
    User = get_user_model()
    q = (request.GET.get("q") or "").strip()

    qs = User.objects.order_by("username")
    if q:
        qs = qs.filter(
            Q(username__icontains=q)
            | Q(email__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
        )

    paginator = Paginator(qs, PAGE_SIZE)
    page = request.GET.get("page") or 1
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    user_ids = [u.id for u in page_obj.object_list]
    aff_map = {
        a.user_id: list(a.affiliations or [])
        for a in UserAffiliation.objects(user_id__in=user_ids)
    }

    items = []
    for u in page_obj.object_list:
        items.append({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "full_name": f"{u.first_name} {u.last_name}".strip(),
            "is_active": u.is_active,
            "is_superuser": u.is_superuser,
            "date_joined": u.date_joined,
            "affiliations": aff_map.get(u.id, []),
        })

    ctx = {
        "items": items,
        "page_obj": page_obj,
        "filter_q": q,
    }
    if page_obj.has_previous():
        ctx["qs_prev"] = _collection_query_string(request, page=page_obj.previous_page_number())
    if page_obj.has_next():
        ctx["qs_next"] = _collection_query_string(request, page=page_obj.next_page_number())
    return render(request, "catalog/data_management/users.html", ctx)


@superuser_required
@require_http_methods(["GET", "POST"])
def data_management_user_edit(request, user_id):
    User = get_user_model()
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise Http404("User not found")

    aff_doc = UserAffiliation.objects(user_id=user_id).first()
    current_affiliations = list(aff_doc.affiliations or []) if aff_doc else []

    errors = {}
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        is_active = request.POST.get("is_active") == "1"
        new_affiliations = request.POST.getlist("affiliations")

        if not username:
            errors["username"] = "Username cannot be empty."
        elif User.objects.exclude(pk=user_id).filter(username=username).exists():
            errors["username"] = "Username already taken."

        if not errors:
            user.username = username
            user.email = email
            user.first_name = first_name
            user.last_name = last_name
            user.is_active = is_active
            user.save()
            upsert_user_affiliations(user, new_affiliations)

            next_url = (request.POST.get("next") or "").strip()
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect("data_management_users")

        current_affiliations = new_affiliations

    next_url = (request.GET.get("next") or "").strip()
    return render(request, "catalog/data_management/user_edit.html", {
        "edit_user": user,
        "all_affiliations": sorted(AFFILIATION_VALUES),
        "current_affiliations": current_affiliations,
        "errors": errors,
        "next_url": next_url,
    })
