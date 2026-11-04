from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django_htmx.http import HttpResponseClientRedirect

from .accounts import has_current_consent
from .ai.base import AIError

# Reading the policies, signing out and leaving must never depend on accepting them.
_CONSENT_FREE = ("/legal/", "/accounts/", "/me/settings", "/me/export", "/me/delete", "/static/", "/healthz", "/readyz")


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
