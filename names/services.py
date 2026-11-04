"""What families can do with a board. Views translate HTTP; everything else lives here."""

from dataclasses import dataclass

from django.db import transaction

from .ai.base import AIError, NameIdea, SuggestionRequest, get_ai
from .dedup import ALMOST_CERTAINLY_SAME, closest, find_duplicate, phonetic_key
from .models import Board, Name, Vote


@dataclass(frozen=True)
class Voter:
    id: str
    name: str
    is_parent: bool


@dataclass(frozen=True)
class AddResult:
    name: Name | None = None
    duplicate: Name | None = None


def _build_name(board: Board, idea: NameIdea, **fields) -> Name:
    return Name(
        board=board,
        text=idea.name[:60],
        key=phonetic_key(idea.name)[:60],
        native=idea.native_script[:60],
        meaning=idea.meaning[:300],
        origin=idea.origin[:80],
        respelling=idea.respelling[:80],
        speech_lang=idea.speech_lang[:20],
        surname_fit=idea.surname_fit[:200],
        **fields,
    )


def suggest_names(board: Board, more_like: str = "") -> list[Name]:
    existing = list(board.names.all())
    request = SuggestionRequest(
        brief=board.brief,
        liked=[n.text for n in existing if n.status == Name.Status.KEPT],
        passed=[n.text for n in existing if n.status == Name.Status.PASSED],
        more_like=more_like,
    )
    fresh: list[Name] = []
    for idea in get_ai().suggest(request):
        name = _build_name(board, idea)
        if closest(name.key, [*existing, *fresh], ALMOST_CERTAINLY_SAME) is None:
            fresh.append(name)
    return Name.objects.bulk_create(fresh)


def _describe(board: Board, text: str) -> NameIdea:
    surname = board.brief.get("surname", "")
    try:
        return get_ai().describe(text, surname).model_copy(update={"name": text})
    except AIError:
        return NameIdea(
            name=text, native_script="", meaning="", origin="", respelling="", speech_lang="", surname_fit=""
        )


def add_name(board: Board, text: str, voter: Voter, *, force: bool = False) -> AddResult:
    duplicate = None if force else find_duplicate(text, board.names.all(), _judge())
    if duplicate:
        return AddResult(duplicate=duplicate)
    name = _build_name(board, _describe(board, text), suggested_by=voter.name)
    name.save()
    if voter.is_parent:
        keep(name, voter)
    return AddResult(name=name)


def _judge():
    try:
        return get_ai()
    except AIError:
        return _Undecided()


class _Undecided:
    def same_name(self, name_a: str, name_b: str) -> bool:
        raise AIError


def merge_into(name: Name, text: str, voter: Voter) -> None:
    if text != name.text and text not in name.variants:
        name.variants.append(text)
        name.save(update_fields=["variants"])
    cast_vote(name, voter)


def cast_vote(name: Name, voter: Voter) -> None:
    Vote.objects.update_or_create(
        name=name,
        voter=voter.id,
        defaults={"voter_name": voter.name, "is_parent": voter.is_parent},
    )


def toggle_vote(name: Name, voter: Voter) -> None:
    deleted, _ = name.votes.filter(voter=voter.id).delete()
    if not deleted:
        cast_vote(name, voter)


def keep(name: Name, voter: Voter) -> None:
    name.status = Name.Status.KEPT
    name.rank = name.board.names.filter(status=Name.Status.KEPT).count()
    name.save(update_fields=["status", "rank"])
    cast_vote(name, voter)


def pass_on(name: Name) -> None:
    name.status = Name.Status.PASSED
    name.save(update_fields=["status"])


def set_order(board: Board, name_ids: list[int]) -> None:
    names = board.names.filter(pk__in=name_ids).in_bulk()
    for rank, name_id in enumerate(name_ids):
        if name_id in names:
            names[name_id].rank = rank
    Name.objects.bulk_update(names.values(), ["rank"])


def make_official(name: Name, variant: str) -> None:
    if variant in name.variants:
        name.variants = [name.text if v == variant else v for v in name.variants]
        name.text = variant
        name.save(update_fields=["text", "variants"])


@transaction.atomic
def split_variant(name: Name, variant: str, voter: Voter) -> None:
    """Undo a merge: the variant was a different name after all."""
    if variant in name.variants:
        name.variants.remove(variant)
        name.save(update_fields=["variants"])
        add_name(name.board, variant, voter, force=True)


def pronunciation_audio(name: Name) -> bytes:
    if name.audio is None:
        name.audio = get_ai().speak(name.native or name.text)
        name.save(update_fields=["audio"])
    return bytes(name.audio)


def shortlist(board: Board, voter_id: str) -> list[Name]:
    names = list(board.names.filter(status=Name.Status.KEPT).prefetch_related("votes").defer("audio"))
    for name in names:
        name.mine = any(vote.voter == voter_id for vote in name.votes.all())
    return names
