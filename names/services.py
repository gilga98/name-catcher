"""What families can do with a board. Views translate HTTP; everything else lives here."""

from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import transaction

from . import accounts, catalog, soota
from .ai.base import AIError, NameIdea, SuggestionRequest, get_ai
from .ai.context import brief_for_llm
from .dedup import (
    ALMOST_CERTAINLY_SAME,
    closest,
    find_duplicate,
    looks_like_name,
    phonetic_key,
    same_name,
    speech_language,
)
from .jobs import LLM, SOOTA, enqueue
from .models import AICall, Board, CatalogName, ConsentRecord, Job, Name, Swipe, Vote, new_token

UNSPEAKABLE = "The natural voice isn't available for this name. Use your device's voice."


@dataclass(frozen=True)
class Voter:
    id: str
    name: str
    is_parent: bool


@dataclass(frozen=True)
class AddResult:
    name: Name | None = None
    duplicate: Name | None = None
    hidden_duplicate: bool = False


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
        key=catalog.canonical_key(idea.name),
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
        # A bare uploaded name has no details yet. Leaving this blank asks the next LLM batch to fill it in.
        respelling=(entry.respelling or entry.text) if entry.meaning else "",
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


def birth_star_job(board: Board) -> Job | None:
    """A finished reading deletes its job, so a row here means it's still running, or that it failed."""
    return Job.objects.filter(board=board, kind="read_birth_star").order_by("-pk").first()


def start_birth_star(board: Board, user: AbstractBaseUser, birth: dict) -> None:
    job = birth_star_job(board)
    if board.birth_star or (job and job.status != Job.Status.FAILED):
        return
    if job:
        job.delete()
    accounts.record_consent(user, ConsentRecord.Purpose.BIRTH_STAR)
    enqueue(SOOTA, "read_birth_star", {"birth": birth}, board=board)
    board.refresh_from_db(fields=["birth_star"])


def remove_birth_star(board: Board, user: AbstractBaseUser) -> None:
    board.birth_star = {}
    board.save(update_fields=["birth_star"])
    accounts.record_consent(user, ConsentRecord.Purpose.BIRTH_STAR, ConsentRecord.Action.WITHDRAWN)


def birth_sounds(board: Board) -> list[str]:
    """Phonetic keys, so they can be compared with any name's key, whatever its spelling or script."""
    letters = board.birth_star.get("letters", [])
    return [key for key in map(phonetic_key, letters) if key]


def numerology_on(board: Board) -> bool:
    return bool(settings.SOOTA_API_URL and board.brief.get("numerology"))


def store_name_numbers(board: Board) -> None:
    """Covers every name waiting on this board at once, and the next card to ask picks up any beyond one request."""
    names = [name for name in board.names.exclude(status=Name.Status.PASSED).defer("audio") if name.needs_number]
    names = names[: soota.MAX_NAMES]
    if not names:
        return
    for name, numbers in zip(names, soota.read_name_numbers([name.text for name in names]), strict=True):
        name.numerology = numbers
    Name.objects.bulk_update(names, ["numerology"])


def request_name_number(name: Name) -> bool:
    """True while it's worth a card asking again. After a failure it stops, until the page is next opened."""
    if not numerology_on(name.board) or not name.needs_number:
        return False
    board = name.board
    job = enqueue(SOOTA, "read_name_numbers", {"board": board.pk}, board=board, dedupe=f"numbers-{board.pk}")
    name.refresh_from_db(fields=["numerology"])
    return name.needs_number and job.status != Job.Status.FAILED


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
        "birthSound": phonetic_key(board.birth_star.get("letter", "")),
        "birthSounds": birth_sounds(board),
        "aiAllowed": accounts.ai_allowed(board),
    }


def add_from_catalog(board: Board, ids: list[int]) -> list[Name]:
    """The browser chose these. It is trusted with taste, not with state, so everything is checked again here."""
    entries = catalog.pick(ids[:20])
    names = Name.objects.bulk_create(_fresh([_from_catalog(board, entry) for entry in entries], board))
    kept = {name.key for name in names}
    catalog.record_picks(entry for entry in entries if entry.key in kept)
    prepare_names(board, names)
    return names


def _suggestion_request(board: Board, more_like: str = "") -> SuggestionRequest:
    existing = list(board.names.defer("audio"))
    newest_first = sorted(existing, key=lambda n: n.pk or 0, reverse=True)
    return SuggestionRequest(
        brief=brief_for_llm(board.brief, board.birth_star),
        liked=[n.text for n in newest_first if n.status == Name.Status.KEPT][:30],
        passed=[n.text for n in newest_first if n.status == Name.Status.PASSED][:30],
        more_like=more_like,
        overused=catalog.overused_names(),
        genres=catalog.genre_names(),
        family_suggested=[n.text for n in newest_first if n.suggested_by][:30],
    )


def _family_request(board: Board) -> SuggestionRequest:
    """Relatives read this answer, so the prompt holds only what they can already see. Text they typed stays out too."""
    brief = brief_for_llm(board.brief)
    if not board.brief.get("share_notes"):
        brief.pop("notes", None)
    return SuggestionRequest(
        brief=brief,
        overused=catalog.overused_names(),
        genres=catalog.genre_names(),
    )


def _ask_llm(request: SuggestionRequest) -> list[NameIdea]:
    answers = get_ai().suggest(request)
    ideas = [catalog.tidy_idea(idea) for idea in answers if looks_like_name(idea.name)]
    catalog.remember_ideas(ideas)
    return ideas


def suggest_names(board: Board, more_like: str = "") -> list[Name]:
    """The last resort: the browser found too little in the catalogue, so this one costs money."""
    ideas = _ask_llm(_suggestion_request(board, more_like))
    candidates = [_build_name(board, idea, source=Name.Source.LLM) for idea in ideas]
    names = Name.objects.bulk_create(_fresh(candidates, board))
    prepare_names(board, names)
    return names


def start_suggesting(board: Board, more_like: str = "") -> Job:
    """Checks what's quick here, so limits can be reported at once, and leaves the LLM call to the background."""
    if not accounts.ai_allowed(board):
        raise AIError("AI suggestions are switched off for this board in Settings.")
    active = Job.objects.filter(board=board, kind="suggest", status__in=[Job.Status.QUEUED, Job.Status.RUNNING]).first()
    if active:
        return active
    accounts.charge_ai_call(board, AICall.Kind.SUGGEST)
    return enqueue(LLM, "suggest", {"board": board.pk, "more_like": more_like}, board=board)


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


def fetch_family_ideas(board: Board) -> None:
    try:
        accounts.charge_ai_call(board, AICall.Kind.SUGGEST)
        names = [{"text": i.name[:60], "meaning": i.meaning} for i in _ask_llm(_family_request(board))]
    except AIError:
        names = []  # They can still type their own, so a failure here isn't worth an error.
    board.family_ideas = {"stamp": catalog.brief_stamp(board.brief), "names": names, "pending": False}
    board.save(update_fields=["family_ideas"])


def family_ideas_pending(board: Board) -> bool:
    stored = board.family_ideas
    return bool(stored.get("pending")) and stored.get("stamp") == catalog.brief_stamp(board.brief)


def family_ideas(board: Board, *, allow_llm: bool) -> list[dict]:
    """Ideas for relatives to tap, right away from the catalogue. Any LLM ideas join them when they're ready."""
    if allow_llm and family_ideas_need_llm(board):
        board.family_ideas = {"stamp": catalog.brief_stamp(board.brief), "names": [], "pending": True}
        board.save(update_fields=["family_ideas"])
        enqueue(LLM, "family_ideas", {"board": board.pk}, board=board)
        board.refresh_from_db()
    seen = set(board.names.values_list("key", flat=True))
    ideas = [{"text": n.text, "meaning": n.meaning} for n in catalog.ideas_for(board.brief, seen)]
    ideas += [idea for idea in _stored_family_ideas(board) if phonetic_key(idea["text"]) not in seen]
    return ideas[:8]


def add_name(board: Board, text: str, voter: Voter, *, force: bool = False) -> AddResult:
    """Local and instant. describe_name fills in the details afterwards, so nobody waits on the LLM."""
    key = catalog.canonical_key(text)
    duplicate = None if force else find_duplicate(text, board.names.defer("audio"), key=key)
    if duplicate and not voter.is_parent:
        # "Is this the same as...?" would give away what's on the board, so their support joins it without asking.
        merge_into(duplicate, text, voter)
        return AddResult(hidden_duplicate=True)
    if duplicate:
        return AddResult(duplicate=duplicate)
    language = speech_language(text)
    name = Name.objects.create(
        board=board,
        text=text,
        key=key,
        native=text if language else "",
        speech_lang=language,
        suggested_by=voter.name,
    )
    if voter.is_parent:
        keep(name, voter)
    prepare_names(name.board, [name])
    return AddResult(name=name)


class Details:
    READY, PENDING, UNAVAILABLE = "ready", "pending", "unavailable"


def _copy_details(name: Name, entry: CatalogName) -> None:
    details = _from_catalog(name.board, entry)
    name.meaning, name.origin = details.meaning, details.origin
    if details.native:
        name.native = details.native
    # Someone reading only Latin should be able to read this. What they typed stays as its native spelling.
    if not name.text.isascii() and entry.text.isascii():
        name.native, name.text = name.text, entry.text
    name.respelling = details.respelling or name.text
    # The voice has to match the script on the card, which only we know for certain.
    name.speech_lang = speech_language(name.native) or details.speech_lang or name.speech_lang
    name.surname_fit, name.insight, name.tags = details.surname_fit, details.insight, details.tags
    name.save(
        update_fields=[
            "text", "native", "meaning", "origin", "respelling", "speech_lang", "surname_fit", "insight", "tags",
        ]  # fmt: skip
    )


def describe_name(name: Name) -> str:
    """Fills in a card straight away if anyone has ever asked about this name, otherwise queues it for a batch."""
    if name.respelling:
        return Details.READY
    entry = catalog.lookup(name.key)
    if entry:
        _copy_details(name, entry)
        return Details.READY
    if accounts.ai_allowed(name.board) and catalog.request_enrichment(name.text, name.key):
        name.refresh_from_db()
        return Details.READY if name.respelling else Details.PENDING
    return Details.UNAVAILABLE


def prepare_names(board: Board, names: list[Name]) -> None:
    """Starts every third-party call a card could need as soon as a name arrives, so nobody waits on one later."""
    for name in names:
        describe_name(name)
    if accounts.ai_allowed(board):
        catalog.request_voices([name.key for name in names])
    for name in names:
        if request_name_number(name):
            break  # One request covers every name waiting on the board.


def fill_boards(entry: CatalogName) -> None:
    """One LLM answer reaches every family waiting on that name."""
    waiting = list(Name.objects.filter(key=entry.key, respelling="").select_related("board").defer("audio"))
    for name in waiting:
        _copy_details(name, entry)
    if waiting:
        catalog.request_voices([entry.key])  # Spelling merges can move a name to this entry after it was first asked.


def merge_into(name: Name, text: str, voter: Voter) -> bool:
    """A variant can become the official spelling, so it has to really be this name. False if it isn't."""
    if catalog.canonical_key(text) != name.key and not same_name(text, name.key):
        return False
    if text != name.text and text not in name.variants:
        name.variants.append(text)
        name.save(update_fields=["variants"])
    cast_vote(name, voter)
    return True


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


def _partner_labels(board: Board) -> dict[str, str]:
    members = board.members.select_related("profile")
    labels = {}
    for member in members:
        profile = getattr(member, "profile", None)
        labels[accounts.voter_id(member)] = (profile.display_name if profile else "") or "Your partner"
    return labels


def _waiting_note(name: Name, voter: Voter, labels: dict[str, str]) -> str:
    partner = next((text for voter_id, text in labels.items() if voter_id != voter.id), "")
    kept_it = any(swipe.voter == voter.id and swipe.liked for swipe in name.swipes.all())
    return f"Waiting for {partner}" if partner and kept_it else ""


def _note_for(name: Name, voter: Voter, labels: dict[str, str]) -> None:
    """Tells this parent what their partner decided, or who they're waiting on."""
    theirs = next((swipe for swipe in name.swipes.all() if swipe.voter != voter.id), None)
    name.partner_kept = theirs is not None and theirs.liked
    name.partner_passed = theirs is not None and not theirs.liked
    if theirs is None:
        name.partner_note = _waiting_note(name, voter, labels)
        return
    label = labels.get(theirs.voter, "Your partner")
    name.partner_note = f"{label} kept this" if theirs.liked else f"{label} passed on this"


def with_partner_notes(names: list[Name], board: Board, voter: Voter) -> list[Name]:
    labels = _partner_labels(board)
    for name in names:
        _note_for(name, voter, labels)
    return names


def deck_for(board: Board, voter: Voter) -> list[Name]:
    """Everything still alive that this parent hasn't decided on, including what their partner already decided."""
    names = board.names.exclude(status=Name.Status.PASSED).defer("audio").prefetch_related("swipes")
    live = [n for n in names if n.status == Name.Status.REVIEW or n.swipes.all()]
    undecided = [n for n in live if not any(s.voter == voter.id for s in n.swipes.all())]
    return with_partner_notes(undecided, board, voter)


def cards(board: Board, voter: Voter, ids: list[int]) -> list[Name]:
    """Names that reached this parent's deck since the page loaded, such as a relative's new suggestion."""
    return [name for name in deck_for(board, voter) if name.pk in ids]


def decisions(board: Board, voter: Voter) -> dict:
    """A cheap summary browsers poll, so a partner's decisions show up without a reload."""
    deck = deck_for(board, voter)
    theirs = Swipe.objects.filter(name__board=board).exclude(voter=voter.id).order_by("pk").values_list("pk", flat=True)
    pks = list(theirs)
    return {
        "notes": {name.pk: {"text": name.partner_note, "kept": name.partner_kept} for name in deck},
        "stamp": f"{len(pks)}-{pks[-1] if pks else 0}",
    }


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


def cached_audio(name: Name) -> bytes | None:
    if name.audio is not None:
        return bytes(name.audio)
    shared = CatalogName.objects.filter(key=name.key, audio__isnull=False).first()
    if shared is None:
        return None
    name.audio = shared.audio
    name.save(update_fields=["audio"])
    return bytes(shared.audio)


def speech_underway(name: Name) -> bool:
    return Job.objects.filter(dedupe=f"speak:{name.key}", status__in=[Job.Status.QUEUED, Job.Status.RUNNING]).exists()


def start_speaking(name: Name) -> Job:
    """Everyone asking for this name's voice at once shares one job, and the recording, for good."""
    if not accounts.ai_allowed(name.board):
        raise AIError("The natural voice uses AI, which is switched off for this board. Use your device's voice.")
    if not speech_underway(name):
        accounts.charge_ai_call(name.board, AICall.Kind.SPEAK)
    return enqueue(LLM, "speak", {"name": name.pk}, board=name.board, dedupe=f"speak:{name.key}")


def _speech_text(name: Name | CatalogName) -> str:
    """The recording is shared by everyone with this key, so what's spoken has to sound like that key."""
    for text in (name.native, name.text):
        if text and (catalog.canonical_key(text) == name.key or same_name(text, name.key)):
            return text
    raise AIError(UNSPEAKABLE)


def record_audio(name: Name) -> None:
    if cached_audio(name) is not None:  # The pipeline got there while this job was waiting.
        return
    audio = get_ai().speak(_speech_text(name))
    CatalogName.objects.get_or_create(key=name.key, defaults={"text": name.text, "source": CatalogName.Source.LLM})
    CatalogName.objects.filter(key=name.key).update(audio=audio, voice_state=CatalogName.Enrich.DONE)
    name.audio = audio
    name.save(update_fields=["audio"])


def record_catalog_voice(entry: CatalogName) -> None:
    """Boards pick this up through cached_audio the first time someone taps."""
    audio = get_ai().speak(_speech_text(entry))
    CatalogName.objects.filter(pk=entry.pk).update(audio=audio, voice_state=CatalogName.Enrich.DONE)


def shortlist(board: Board, voter: Voter) -> list[Name]:
    """Parents see everything either of them kept. Family only suggest, so they see none of it."""
    if not voter.is_parent:
        return []
    names = list(board.names.filter(status=Name.Status.KEPT).prefetch_related("votes", "swipes").defer("audio"))
    with_partner_notes(names, board, voter)
    for name in names:
        name.mine = any(vote.voter == voter.id for vote in name.votes.all())
        name.split = len({swipe.liked for swipe in name.swipes.all()}) == 2
    return sorted(names, key=lambda name: name.split)
