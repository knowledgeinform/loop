"""Relational models used by Django-facing LOOP infrastructure."""

import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


class APIKey(models.Model):
    """A revocable, scoped API credential whose secret is never stored."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="loop_api_keys"
    )
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=20, unique=True, db_index=True)
    key_hash = models.CharField(max_length=64)
    scopes = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["expires_at"])]

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, *, user, name: str, scopes: list[str], expires_at=None):
        prefix = secrets.token_hex(6)
        raw_key = f"loop_{prefix}_{secrets.token_urlsafe(32)}"
        obj = cls.objects.create(
            user=user,
            name=name,
            prefix=prefix,
            key_hash=cls.hash_key(raw_key),
            scopes=list(scopes),
            expires_at=expires_at,
        )
        return obj, raw_key

    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()

    def matches(self, raw_key: str) -> bool:
        return secrets.compare_digest(self.key_hash, self.hash_key(raw_key))
