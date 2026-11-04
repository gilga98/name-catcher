"""What families can do with a board. Views translate HTTP; everything else lives here."""

from dataclasses import dataclass

from django.contrib.auth.base_user import AbstractBaseUser
from django.db import transaction

from . import accounts, catalog
from .ai.base import AIError, NameIdea, SuggestionRequest, get_ai
from .ai.context import brief_for_llm
from .dedup import ALMOST_CERTAINLY_SAME, closest, find_duplicate, phonetic_key
from .models import AICall, Board, CatalogName, Name, Swipe, Vote, new_token


@dataclass(frozen=True)
class Voter:
    id: str
    name: str
    is_parent: bool


@dataclass(frozen=True)
class AddResult:
    name: Name | None = None
    duplicate: Name | None = None


@transaction.atomic
def create_board(user: AbstractBaseUser, brief: dict) -> Board:
    board = Board.objects.create(brief=brief)
    board.members.add(user)
    return board


@transaction.atomic
def join_board(board: Board, user: AbstractBaseUser) -> None:
    """Invites are single use: each join issues a new link."""
    board.members.add(user)
    board.invite_token = new_token()
    board.save(update_fields=["invite_token"])
    # A name one parent binned alone isn't rejected by both any more, so the new parent gets to decide on it.
    for name in board.names.filter(swipes__isnull=False).distinct().defer("audio"):
        name.settle()


def reset_family_link(board: Board) -> None:
    board.guest_token = new_token()
    board.save(update_fields=["guest_token"])


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
        surname_fit=idea.surname_fit[:200] or catalog.surname_fit(idea.name, board.brief.get("surname", "")),
        insight=idea.insight[:200],
        tags=catalog.tags_for(idea.origin[:30], idea.rarity, idea.style, idea.genres),
        **fields,
    )


def _from_catalog(board: Board, entry: CatalogName) -> Name:
    return Name(
        board=board,
        text=entry.text,
        key=entry.key,
        native=entry.native,
        meaning=entry.meaning,
        origin=entry.origin,
        respelling=entry.respelling or entry.text,
        speech_lang=entry.speech_lang,
        surname_fit=catalog.surname_fit(entry.text, board.brief.get("surname", "")),
        insight=entry.insight,
        tags=catalog.tags_for(entry.origin, entry.rarity, entry.style, [g.name for g in entry.genres.all()]),
        source=Name.Source.CATALOG,
    )


def _avoided(board: Board) -> list[str]:
    parts = board.brief.get("avoid", "").replace(",", " ").split()
    return [key for key in map(phonetic_key, parts) if key]


class _Keyed:
    def __init__(self, key: str):
        self.key = key


def _fresh(candidates: list[Name], board: Board) -> list[Name]:
    """Drop anything the family has seen or asked to avoid, across spellings and scripts."""
    pool = [*board.names.defer("audio"), *map(_Keyed, _avoided(board))]
    fresh: list[Name] = []
    for name in candidates:
        if closest(name.key, [*pool, *fresh], ALMOST_CERTAINLY_SAME) is None:
            fresh.append(name)
    return fresh


def engine_context(board: Board, voter: Voter) -> dict:
    """What the browser needs to pick names itself. Holds keys rather than text, and never the family name."""
    mine = Swipe.objects.filter(name__board=board, voter=voter.id).values_list("name__key", "liked")
    return {
        "liked": [key for key, liked in mine if liked],
        "passed": [key for key, liked in mine if not liked],
        "seen": list(board.names.values_list("key", flat=True)),
        "avoid": _avoided(board),
        "themes": board.brief.get("themes", []),
        "uniqueness": board.brief.get("uniqueness") or "any",
        "startsWith": phonetic_key(board.brief.get("starts_with", "")),
        "aiAllowed": accounts.ai_allowed(board),
    }


def add_from_catalog(board: Board, ids: list[int]) -> list[Name]:
    """The browser chose these. It is trusted with taste, not with state, so everything is checked again here."""
    entries = catalog.pick(ids[:20])
    names = Name.objects.bulk_create(_fresh([_from_catalog(board, entry) for entry in entries], board))
    kept = {name.key for name in names}
    catalog.record_picks(entry for entry in entries if entry.key in kept)
    return names


def _suggestion_request(board: Board, more_like: str = "") -> SuggestionRequest:
    existing = list(board.names.defer("audio"))
    newest_first = sorted(existing, key=lambda n: n.pk or 0, reverse=True)
    return SuggestionRequest(
        brief=brief_for_llm(board.brief),
        liked=[n.text for n in newest_first if n.status == Name.Status.KEPT][:30],
        passed=[n.text for n in newest_first if n.status == Name.Status.PASSED][:30],
        more_like=more_like,
        overused=catalog.overused_names(),
        genres=catalog.genre_names(),
        family_suggested=[n.text for n in newest_first if n.suggested_by][:30],
    )


def _ask_llm(board: Board, more_like: str = "") -> list[NameIdea]:
    accounts.charge_ai_call(board, AICall.Kind.SUGGEST)
    ideas = [catalog.tidy_idea(idea) for idea in get_ai().suggest(_suggestion_request(board, more_like))]
    catalog.remember_ideas(ideas)
    return ideas


def suggest_names(board: Board, more_like: str = "") -> list[Name]:
    """The last resort: the browser found too little in the catalogue, so this one costs money."""
    if not accounts.ai_allowed(board):
        raise AIError("AI suggestions are switched off for this board in Settings.")
    candidates = [_build_name(board, idea, source=Name.Source.LLM) for idea in _ask_llm(board, more_like)]
    return Name.objects.bulk_create(_fresh(candidates, board))


MIN_FAMILY_IDEAS = 4


def _stored_family_ideas(board: Board) -> list[dict]:
    stored = board.family_ideas
    return stored["names"] if stored.get("stamp") == catalog.brief_stamp(board.brief) else []


def family_ideas_need_llm(board: Board) -> bool:
    """At most one LLM call per version of the brief, however many relatives open the link."""
    if board.family_ideas.get("stamp") == catalog.brief_stamp(board.brief) or not accounts.ai_allowed(board):
        return False
    seen = set(board.names.values_list("key", flat=True))
    return len(catalog.ideas_for(board.brief, seen, MIN_FAMILY_IDEAS)) < MIN_FAMILY_IDEAS


def family_ideas(board: Board, *, allow_llm: bool) -> list[dict]:
    """Ideas for relatives to tap. They can't spend money directly: this is cached on the board."""
    if allow_llm and family_ideas_need_llm(board):
        try:
            names = [{"text": i.name[:60], "meaning": i.meaning} for i in _ask_llm(board)]
        except AIError:
            names = []  # They can still type their own, so a failure here isn't worth an error.
        board.family_ideas = {"stamp": catalog.brief_stamp(board.brief), "names": names}
        board.save(update_fields=["family_ideas"])
    seen = set(board.names.values_list("key", flat=True))
    ideas = [{"text": n.text, "meaning": n.meaning} for n in catalog.ideas_for(board.brief, seen)]
    ideas += [idea for idea in _stored_family_ideas(board) if phonetic_key(idea["text"]) not in seen]
    return ideas[:8]


def add_name(board: Board, text: str, voter: Voter, *, force: bool = False) -> AddResult:
    """Local and instant. describe_name fills in the details afterwards, so nobody waits on the LLM."""
    duplicate = None if force else find_duplicate(text, board.names.all())
    if duplicate:
        return AddResult(duplicate=duplicate)
    name = Name.objects.create(board=board, text=text, key=phonetic_key(text)[:60], suggested_by=voter.name)
    if voter.is_parent:
        keep(name, voter)
    return AddResult(name=name)


def details_need_llm(name: Name) -> bool:
    return not name.respelling and catalog.lookup(name.key) is None


def describe_name(name: Name) -> bool:
    """Fills in the details, from the catalogue if it can. False means nobody could: AI is off for this board."""
    if name.respelling:
        return True
    entry = catalog.lookup(name.key)
    if entry:
        details = _from_catalog(name.board, entry)
    elif accounts.ai_allowed(name.board):
        accounts.charge_ai_call(name.board, AICall.Kind.DESCRIBE)
        idea = catalog.tidy_idea(get_ai().describe(name.text, catalog.genre_names()))
        catalog.remember_ideas([idea])
        details = _build_name(name.board, idea)
    else:
        return False
    name.native, name.meaning, name.origin = details.native, details.meaning, details.origin
    name.respelling = details.respelling or name.text
    name.speech_lang, name.surname_fit = details.speech_lang, details.surname_fit
    name.insight, name.tags = details.insight, details.tags
    name.save(
        update_fields=["native", "meaning", "origin", "respelling", "speech_lang", "surname_fit", "insight", "tags"]
    )
    return True


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


def _record_swipe(name: Name, voter: Voter, *, liked: bool) -> None:
    Swipe.objects.update_or_create(name=name, voter=voter.id, defaults={"liked": liked})
    name.settle()


def keep(name: Name, voter: Voter) -> None:
    name.rank = name.board.names.filter(status=Name.Status.KEPT).count()
    name.save(update_fields=["rank"])
    _record_swipe(name, voter, liked=True)
    cast_vote(name, voter)


def pass_on(name: Name, voter: Voter) -> None:
    name.votes.filter(voter=voter.id).delete()
    _record_swipe(name, voter, liked=False)


def undo_swipe(name: Name, voter: Voter) -> None:
    name.votes.filter(voter=voter.id).delete()
    name.swipes.filter(voter=voter.id).delete()
    name.status = Name.Status.REVIEW
    name.save(update_fields=["status"])
    name.settle()


def bring_back(name: Name) -> None:
    """Out of the bin, and both parents get to look at it again."""
    name.swipes.all().delete()
    name.status = Name.Status.REVIEW
    name.save(update_fields=["status"])


def deck_for(board: Board, voter: Voter) -> list[Name]:
    """Everything still alive that this parent hasn't decided on, including what their partner already liked."""
    names = board.names.exclude(status=Name.Status.PASSED).defer("audio").prefetch_related("swipes")
    decided = [n for n in names if n.status == Name.Status.REVIEW or n.swipes.all()]
    return [n for n in decided if not any(s.voter == voter.id for s in n.swipes.all())]


def binned(board: Board) -> list[Name]:
    return list(board.names.filter(status=Name.Status.PASSED).defer("audio"))


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


def audio_needs_llm(name: Name) -> bool:
    return name.audio is None and not CatalogName.objects.filter(key=name.key, audio__isnull=False).exists()


def pronunciation_audio(name: Name) -> bytes:
    """Recordings are shared by every family through the catalogue, so each name is only ever paid for once."""
    if name.audio is not None:
        return bytes(name.audio)
    shared = CatalogName.objects.filter(key=name.key).first()
    if shared is None or shared.audio is None:
        if not accounts.ai_allowed(name.board):
            raise AIError("The natural voice uses AI, which is switched off for this board. Use your device's voice.")
        accounts.charge_ai_call(name.board, AICall.Kind.SPEAK)
        audio = get_ai().speak(name.native or name.text)
        CatalogName.objects.filter(key=name.key).update(audio=audio)
    else:
        audio = bytes(shared.audio)
    name.audio = audio
    name.save(update_fields=["audio"])
    return audio


def _visible_on_shortlist(name: Name, voter: Voter) -> bool:
    """Decisions stay blind: nobody learns what their partner thought until they've decided themselves."""
    swipes = name.swipes.all()
    if not voter.is_parent:
        return all(swipe.liked for swipe in swipes)
    return not swipes or any(swipe.voter == voter.id for swipe in swipes)


def shortlist(board: Board, voter: Voter) -> list[Name]:
    kept = board.names.filter(status=Name.Status.KEPT).prefetch_related("votes", "swipes").defer("audio")
    names = [name for name in kept if _visible_on_shortlist(name, voter)]
    for name in names:
        name.mine = any(vote.voter == voter.id for vote in name.votes.all())
        name.split = len({swipe.liked for swipe in name.swipes.all()}) == 2
    return sorted(names, key=lambda name: name.split)
