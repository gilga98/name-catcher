from django.conf import settings


def _published(setting: str) -> str:
    """A missing detail shows as an obvious placeholder, so an unfinished policy can't pass for a finished one."""
    return getattr(settings, setting) or f"[set {setting}]"


def legal(request) -> dict:
    return {
        "legal": {
            "entity": _published("LEGAL_ENTITY_NAME"),
            "address": _published("LEGAL_ADDRESS"),
            "city": _published("LEGAL_JURISDICTION_CITY"),
            "officer": _published("GRIEVANCE_OFFICER_NAME"),
            "email": _published("GRIEVANCE_EMAIL"),
            "version": settings.POLICY_VERSION,
            "retention_months": settings.ACCOUNT_RETENTION_DAYS // 30,
        }
    }
