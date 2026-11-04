"""What `manage.py check --deploy` should say before launch. Each of these leaves a hole only production can have."""

from django.conf import settings
from django.core.checks import Tags, Warning, register

_REQUIRED = (
    ("REDIS_URL", "Without it, each worker keeps its own rate-limit counters, and a restart clears them."),
    ("ALLAUTH_TRUSTED_CLIENT_IP_HEADER", "Set TRUSTED_CLIENT_IP_HEADER, or every visitor shares one proxy address."),
    ("CF_ACCESS_TEAM_DOMAIN", "Without it, the admin panel stays closed."),
    ("CF_ACCESS_AUD", "Without it, the admin panel stays closed."),
    ("TURNSTILE_SITE_KEY", "Without it, nothing stops bots signing up."),
    ("TURNSTILE_SECRET_KEY", "Without it, nothing stops bots signing up."),
)


@register(Tags.security, deploy=True)
def launch_settings(app_configs, **kwargs) -> list[Warning]:
    return [
        Warning(f"{name} is not set.", hint=hint, id=f"names.W{number:03}")
        for number, (name, hint) in enumerate(_REQUIRED, start=1)
        if not getattr(settings, name, "")
    ]
