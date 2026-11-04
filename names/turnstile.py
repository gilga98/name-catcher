"""Cloudflare Turnstile: a check that a person is signing up. Off until its keys are set."""

import logging

import httpx
from django.conf import settings
from django.forms import Widget
from django.utils.html import format_html

logger = logging.getLogger(__name__)

RESPONSE_FIELD = "cf-turnstile-response"
_VERIFY_URL = f"{settings.TURNSTILE_ORIGIN}/turnstile/v0/siteverify"
_TIMEOUT_SECONDS = 5


class TurnstileWidget(Widget):
    def render(self, name, value, attrs=None, renderer=None) -> str:
        return format_html(
            '<div class="cf-turnstile" data-sitekey="{}"></div><script src="{}" async defer></script>',
            settings.TURNSTILE_SITE_KEY,
            f"{settings.TURNSTILE_ORIGIN}/turnstile/v0/api.js",
        )


def is_person(token: str) -> bool:
    """If Cloudflare can't answer, this says no: sign-up is what bots would flood the moment the check went quiet."""
    data = {"secret": settings.TURNSTILE_SECRET_KEY, "response": token}
    try:
        return httpx.post(_VERIFY_URL, data=data, timeout=_TIMEOUT_SECONDS).json().get("success") is True
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Turnstile check failed: %s", type(exc).__name__)
        return False
