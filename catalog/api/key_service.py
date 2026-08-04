"""API-key lifecycle shared by website and HTTP API adapters."""

from django.utils import timezone

from catalog.models import APIKey


def describe(key):
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "scopes": list(key.scopes or []),
        "created_at": key.created_at,
        "last_used_at": key.last_used_at,
        "expires_at": key.expires_at,
        "revoked_at": key.revoked_at,
        "is_active": key.is_active(),
    }


def list_for_user(user):
    purge_expired(user=user)
    return [describe(key) for key in APIKey.objects.filter(user=user)]


def issue(*, user, name, scopes, expires_at=None):
    key, raw_key = APIKey.issue(
        user=user,
        name=name,
        scopes=scopes,
        expires_at=expires_at,
    )
    return describe(key), raw_key


def purge_expired(*, user=None, now=None):
    """Permanently remove expired credentials and return the number removed."""
    filters = {"expires_at__lte": now or timezone.now()}
    if user is not None:
        filters["user"] = user
    deleted, _ = APIKey.objects.filter(**filters).delete()
    return deleted


def revoke(*, user, key_id):
    try:
        key = APIKey.objects.filter(pk=key_id, user=user).first()
    except (TypeError, ValueError):
        return False
    if key is None:
        return False
    if key.revoked_at is None:
        key.revoked_at = timezone.now()
        key.save(update_fields=["revoked_at"])
    return True
