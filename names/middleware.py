from functools import lru_cache
from urllib.parse import urlencode

import jwt
from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django_htmx.http import HttpResponseClientRedirect

from .accounts import has_current_consent
from .ai.base import AIError

# Reading the policies, signing out and leaving must never depend on accepting them.
_CONSENT_FREE = ("/legal/", "/accounts/", "/me/settings", "/me/export", "/me/delete", "/static/")


@lru_cache(maxsize=1)
def _access_keys() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"https://{settings.CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs")


def _signed_by_access(token: str) -> bool:
    try:
        jwt.decode(
            token,
            _access_keys().get_signing_key_from_jwt(token).key,
            algorithms=["RS256"],
            audience=settings.CF_ACCESS_AUD,
            issuer=f"https://{settings.CF_ACCESS_TEAM_DOMAIN}",
        )
    except jwt.PyJWTError:
        return False
    return True


class CloudflareAccessMiddleware:
    """Zero Trust only guards traffic through Cloudflare. This stops anyone reaching the server another way."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        guarded = settings.CF_ACCESS_TEAM_DOMAIN and settings.CF_ACCESS_AUD
        if guarded and request.path.startswith(f"/{settings.ADMIN_PATH}"):
            if not _signed_by_access(request.headers.get("Cf-Access-Jwt-Assertion", "")):
                return HttpResponse("Sign in through Cloudflare Access.", status=403, content_type="text/plain")
        return self.get_response(request)


class AIErrorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, AIError):
            return HttpResponse(str(exception), status=502, content_type="text/plain")
        return None


class ConsentGateMiddleware:
    """Signed-in people must have accepted the current policies, including after we publish a new version."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._must_accept(request):
            target = f"{reverse('accept_policies')}?{urlencode({'next': request.get_full_path()})}"
            return HttpResponseClientRedirect(target) if request.htmx else redirect(target)
        return self.get_response(request)

    def _must_accept(self, request) -> bool:
        if not request.user.is_authenticated or request.path.startswith((*_CONSENT_FREE, f"/{settings.ADMIN_PATH}")):
            return False
        if request.session.get("policy_accepted") == settings.POLICY_VERSION:
            return False
        if not has_current_consent(request.user):
            return True
        request.session["policy_accepted"] = settings.POLICY_VERSION
        return False
