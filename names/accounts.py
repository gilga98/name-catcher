"""What people control about their own data: consent, AI use, export and erasure."""

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import transaction
from django.utils import timezone

from .ai.base import AIError
from .models import AICall, Board, CatalogName, ConsentRecord, Profile, Swipe, Vote


class AIQuotaReached(AIError):
    pass


def voter_id(user: AbstractBaseUser) -> str:
    return f"user-{user.pk}"


def profile_of(user: AbstractBaseUser) -> Profile:
    return Profile.objects.get_or_create(user=user)[0]


def record_consent(user: AbstractBaseUser, purpose: str = ConsentRecord.Purpose.TERMS) -> None:
    ConsentRecord.objects.create(user=user, purpose=purpose, policy_version=settings.POLICY_VERSION)


def has_current_consent(user: AbstractBaseUser) -> bool:
    return ConsentRecord.objects.filter(
        user=user, purpose=ConsentRecord.Purpose.TERMS, policy_version=settings.POLICY_VERSION
    ).exists()


def ai_allowed(board: Board) -> bool:
    """If any parent has switched AI off, the board stays local: their brief and names are shared."""
    return not board.members.filter(profile__ai_enabled=False).exists()


def ai_calls_left(user: AbstractBaseUser) -> int:
    """The one place a paid plan will change: entitlement decides this number, nothing else needs to know."""
    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = AICall.objects.filter(user=user, created_at__gte=start).count()
    return max(settings.AI_FREE_CALLS_PER_MONTH - used, 0)


def charge_ai_call(board: Board, kind: str) -> None:
    """Family members have no account, so their requests count against the parent who owns the board."""
    owner = board.members.order_by("pk").first()
    if owner is not None and ai_calls_left(owner) <= 0:
        raise AIQuotaReached("This month's AI requests are used up. Names from our catalogue still work.")
    AICall.objects.create(user=owner, kind=kind)


def export_data(user: AbstractBaseUser) -> dict:
    boards = user.boards.prefetch_related("names__votes", "members")
    return {
        "account": {"email": user.email, "joined": user.date_joined, "display_name": profile_of(user).display_name},
        "settings": {"ai_enabled": profile_of(user).ai_enabled},
        "boards": [_board_export(board) for board in boards],
        "contributed_names": list(
            CatalogName.objects.filter(submitted_by=user).values("text", "meaning", "origin", "status")
        ),
        "consents": list(user.consents.values("purpose", "action", "policy_version", "created_at")),
        "my_decisions": list(
            Swipe.objects.filter(voter=voter_id(user)).values("name__text", "liked", "created_at")
        ),
        "ai_requests": list(user.ai_calls.values("kind", "created_at")),
    }


def _board_export(board: Board) -> dict:
    return {
        "brief": board.brief,
        "created_at": board.created_at,
        "names": [
            {
                "text": name.text,
                "status": name.status,
                "meaning": name.meaning,
                "origin": name.origin,
                "suggested_by": name.suggested_by,
                "votes": len(name.votes.all()),
            }
            for name in board.names.all()
        ],
    }


@transaction.atomic
def delete_account(user: AbstractBaseUser) -> None:
    """Boards go with their last parent; a shared board stays with the other parent, minus this person's votes."""
    shared = []
    for board in user.boards.all():
        if board.members.count() == 1:
            board.delete()
        else:
            shared.append(board)
    Vote.objects.filter(voter=voter_id(user)).delete()
    Swipe.objects.filter(voter=voter_id(user)).delete()
    ConsentRecord.objects.create(
        user=user,
        purpose=ConsentRecord.Purpose.TERMS,
        action=ConsentRecord.Action.WITHDRAWN,
        policy_version=settings.POLICY_VERSION,
    )
    user.delete()  # Consent and AI rows keep their dates but lose the link to a person.
    for board in shared:
        for name in board.names.filter(swipes__isnull=False).distinct().defer("audio"):
            name.settle()
