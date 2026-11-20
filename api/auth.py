"""Bearer keys: a building key or an API key with a scope."""
from django.utils import timezone
from rest_framework import authentication, exceptions

from core.models import ApiKey, Building, hash_key


class KeyPrincipal:
    """request.user for API calls. Exactly one of building / api_key is set."""

    is_authenticated = True

    def __init__(self, building=None, api_key=None):
        self.building = building
        self.api_key = api_key

    def allows(self, scope):
        """Buildings may do everything a building endpoint offers; API keys are limited by scope."""
        if self.building is not None:
            return True
        return self.api_key.allows(scope)

    @property
    def rate_key(self):
        return f"bld{self.building.pk}" if self.building else f"key{self.api_key.pk}"


class BearerKeyAuthentication(authentication.BaseAuthentication):
    def authenticate_header(self, request):
        return "Bearer"

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        digest = hash_key(header[7:].strip())
        now = timezone.now()
        building = Building.objects.filter(key_hash=digest).exclude(key_hash="").first()
        if building:
            Building.objects.filter(pk=building.pk).update(last_seen_at=now)
            return KeyPrincipal(building=building), None
        key = ApiKey.objects.filter(key_hash=digest, revoked_at__isnull=True).first()
        if key:
            ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)
            return KeyPrincipal(api_key=key), None
        raise exceptions.AuthenticationFailed("This key is not valid.")
