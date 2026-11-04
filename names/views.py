import uuid
from functools import wraps

from allauth.account.adapter import get_adapter
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_htmx.http import HttpResponseClientRedirect, HttpResponseClientRefresh
from django_ratelimit.core import is_ratelimited
from django_ratelimit.decorators import ratelimit
from django_ratelimit.exceptions import Ratelimited

from . import accounts, catalog, places, services
from .analytics import allow_web_analytics
from .forms import GENDERS, STYLES, UNIQUENESS, AddNameForm, BirthStarForm, BriefForm
from .jobs import LLM, SOOTA, nudge
from .models import Board, Job, Name
from .services import Voter

PENDING_BRIEF = "pending_brief"
NOT_A_NAME = "Enter just the name, up to 28 letters."
DIFFERENT_NAME = "That looks like a different name. Add it on its own."


def rate_limited(request: HttpRequest, exception: Exception) -> HttpResponse:
    message = "That's a lot of requests for now. Try again in a little while."
    return HttpResponse(message, status=429, content_type="text/plain")


def _board_token(_group: str, request: HttpRequest) -> str:
    return request.resolver_match.kwargs["token"]


def _everyone(_group: str, _request: HttpRequest) -> str:
    return "all"


def _client_ip(_group: str, request: HttpRequest) -> str:
    return get_adapter().get_client_ip(request)


def _ip_on_board(group: str, request: HttpRequest) -> str:
    return f"{_client_ip(group, request)}:{_board_token(group, request)}"


def _spend_ai_budget(request: HttpRequest) -> None:
    """Cap LLM spend per visitor, per board and overall, so a leaked link can't run up the bill.

    The shared limit comes last, and stops counting once a narrower one trips: one person can't use it up for everyone.
    """
    limits = (
        ("ai-ip", _client_ip, settings.AI_RATE_PER_IP),
        ("ai-board", _board_token, settings.AI_RATE_PER_BOARD),
        ("ai-all", _everyone, settings.AI_RATE_GLOBAL),
    )
    for group, key, rate in limits:
        if is_ratelimited(request, group=group, key=key, rate=rate, increment=True):
            raise Ratelimited


def _voter(request: HttpRequest, is_parent: bool) -> Voter:
    if is_parent:
        name = accounts.profile_of(request.user).display_name or "Parent"
        return Voter(id=accounts.voter_id(request.user), name=name, is_parent=True)
    return Voter(id=request.session.get("voter", ""), name=request.session.get("voter_name", "Family"), is_parent=False)


def _admit_guest(request: HttpRequest) -> None:
    """A relative gets an identity on their first suggestion, so just reading a board stores nothing.

    Clearing cookies makes a new voter, so this also limits how many votes one address can invent.
    """
    if request.is_parent or "voter" in request.session:
        return
    rate = settings.GUEST_VOTERS_PER_IP
    if is_ratelimited(request, group="guest-voter", key=_ip_on_board, rate=rate, increment=True):
        raise Ratelimited
    request.session["voter"] = uuid.uuid4().hex
    request.voter = _voter(request, False)


def _send_to_login(request: HttpRequest, board: Board) -> HttpResponse:
    board_url = reverse("board", args=[board.parent_token])
    if request.htmx:
        return HttpResponseClientRedirect(f"{reverse(settings.LOGIN_URL)}?next={board_url}")
    return redirect_to_login(board_url)


def with_board(*, parent_only: bool = False):
    """The family link opens the guest view. Everything else needs a signed-in member of the board."""

    def decorator(view):
        @wraps(view)
        def wrapper(request: HttpRequest, token: str, *args, **kwargs):
            rate = settings.BOARD_RATE_PER_IP
            if is_ratelimited(request, group="board-ip", key=_client_ip, rate=rate, increment=True):
                raise Ratelimited
            board = Board.objects.filter(Q(parent_token=token) | Q(guest_token=token)).first()
            if board is None or (parent_only and token == board.guest_token):
                raise Http404
            is_parent = token == board.parent_token
            if is_parent and not request.user.is_authenticated:
                return _send_to_login(request, board)
            if is_parent and not board.members.filter(pk=request.user.pk).exists():
                raise Http404
            request.board, request.is_parent = board, is_parent
            request.voter = _voter(request, is_parent)
            return view(request, token, *args, **kwargs)

        return wrapper

    return decorator


def _name(request: HttpRequest, pk: int) -> Name:
    return get_object_or_404(request.board.names.defer("audio"), pk=pk)


def _context(request: HttpRequest, **extra) -> dict:
    brief = request.board.brief
    surname = brief.get("surname", "")
    return {
        "board": request.board,
        "token": request.resolver_match.kwargs["token"],
        "is_parent": request.is_parent,
        "surname": surname,
        "baby": brief.get("nickname") or (f"baby {surname}" if surname else "baby"),
        "avatar": brief.get("avatar") or "a1",
        "birth_sounds": services.birth_sounds(request.board) if request.is_parent else [],
        "numerology_on": request.is_parent and services.numerology_on(request.board),
        **extra,
    }


def _shortlist(request: HttpRequest, *, oob: bool = False, template: str = "shortlist") -> HttpResponse:
    names = services.shortlist(request.board, request.voter)
    return render(request, f"names/partials/{template}.html", _context(request, shortlist=names, oob=oob))


def _start_board(request: HttpRequest) -> HttpResponse:
    """A brief filled in before signing up waits in the session, so nobody has to answer twice."""
    if not request.user.is_authenticated:
        return redirect("account_signup")
    if is_ratelimited(request, group="create-user", key="user", rate=settings.BOARD_CREATE_PER_USER, increment=True):
        raise Ratelimited
    board = services.create_board(request.user, request.session.pop(PENDING_BRIEF))
    return redirect("board", token=board.parent_token)


@allow_web_analytics
@require_http_methods(["GET", "POST"])
@ratelimit(group="create", key=_client_ip, rate=settings.BOARD_CREATE_RATE, method="POST", block=True)
def home(request: HttpRequest) -> HttpResponse:
    form = BriefForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        request.session[PENDING_BRIEF] = form.cleaned_data
        return _start_board(request)
    if request.user.is_authenticated and PENDING_BRIEF in request.session:
        return _start_board(request)
    boards = request.user.boards.order_by("-created_at") if request.user.is_authenticated else []
    return render(request, "names/home.html", {"form": form, "boards": boards})


@require_http_methods(["GET", "POST"])
@login_required
def join(request: HttpRequest, token: str) -> HttpResponse:
    board = get_object_or_404(Board, invite_token=token)
    if request.method == "POST":
        services.join_board(board, request.user)
        return redirect("board", token=board.parent_token)
    return render(request, "names/join.html", {"surname": board.brief.get("surname", "")})


def _chosen(choices: list, value: str | None) -> str:
    return "" if value in (None, "", "any") else dict(choices).get(value, "")


def _family_context(brief: dict) -> dict:
    """What relatives see before suggesting. "Names to avoid" stays out: it may well be about them."""
    starts_with = brief.get("starts_with")
    tags = [
        _chosen(GENDERS, brief.get("gender")),
        *brief.get("origins", []),
        _chosen(STYLES, brief.get("style")),
        _chosen(UNIQUENESS, brief.get("uniqueness")),
        f"Starts with {starts_with}" if starts_with else "",
    ]
    return {"notes": brief.get("notes", "") if brief.get("share_notes") else "", "tags": [t for t in tags if t]}


@require_GET
@with_board()
def board_page(request: HttpRequest, token: str) -> HttpResponse:
    board = request.board
    queue = services.deck_for(board, request.voter) if request.is_parent else []
    guest_url = request.build_absolute_uri(reverse("board", args=[board.guest_token]))
    invite_url = request.build_absolute_uri(reverse("join", args=[board.invite_token]))
    context = _context(
        request,
        queue=queue,
        shortlist=services.shortlist(board, request.voter),
        is_new=request.is_parent and not board.names.exists(),
        guest_url=guest_url,
        invite_url=invite_url,
        brief_form=BriefForm(initial=board.brief),
        engine=services.engine_context(board, request.voter) if request.is_parent else None,
        binned=services.binned(board) if request.is_parent else [],
        wishes=_family_context(board.brief),
        catalog_version=catalog.version(board.brief),
        by=request.session.get("voter_name", ""),
        **_birth_star_context(request),
    )
    return render(request, "names/board.html", context)


def _birth_star_context(request: HttpRequest, form: BirthStarForm | None = None) -> dict:
    if not (request.is_parent and settings.SOOTA_API_URL):
        return {"birth_star_enabled": False}
    job = services.birth_star_job(request.board)
    failed = job is not None and job.status == Job.Status.FAILED
    pending = job is not None and not failed
    if pending:
        nudge(SOOTA)
    return {
        "birth_star_enabled": True,
        "birth_star_form": form or BirthStarForm(),
        "birth_star_pending": pending,
        "birth_star_error": job.result.get("error") if failed else "",
    }


def _birth_star_panel(request: HttpRequest, form: BirthStarForm | None = None) -> HttpResponse:
    if form is None and request.board.birth_star:
        return HttpResponseClientRefresh()  # The deck reads the new sounds when the page loads.
    context = _birth_star_context(request, form)
    return render(request, "names/partials/birth_star.html", _context(request, **context))


def _require_soota() -> None:
    if not settings.SOOTA_API_URL:
        raise Http404


@require_GET
@with_board(parent_only=True)
def birth_star(request: HttpRequest, token: str) -> HttpResponse:
    _require_soota()
    return _birth_star_panel(request)


@require_POST
@with_board(parent_only=True)
@ratelimit(group="birth-star", key=_board_token, rate=settings.BIRTH_STAR_RATE, block=True)
def read_birth_star(request: HttpRequest, token: str) -> HttpResponse:
    _require_soota()
    form = BirthStarForm(request.POST)
    if not form.is_valid():
        return _birth_star_panel(request, form)
    services.start_birth_star(request.board, request.user, form.birth_input())
    return _birth_star_panel(request)


@require_POST
@with_board(parent_only=True)
@ratelimit(group="place-search", key=_board_token, rate=settings.PLACE_SEARCH_RATE, block=True)
def place_search(request: HttpRequest, token: str) -> JsonResponse:
    """A POST, because a birth place doesn't belong in a URL, where logs would keep it."""
    _require_soota()
    try:
        return JsonResponse({"places": places.search(request.POST.get("q", ""))})
    except places.PlaceSearchError:
        return JsonResponse({"error": "unavailable"}, status=503)


@require_POST
@with_board(parent_only=True)
def remove_birth_star(request: HttpRequest, token: str) -> HttpResponse:
    _require_soota()
    services.remove_birth_star(request.board, request.user)
    return HttpResponseClientRefresh()


@require_POST
@with_board(parent_only=True)
def update_brief(request: HttpRequest, token: str) -> HttpResponse:
    form = BriefForm(request.POST)
    if form.is_valid():
        request.board.brief = form.cleaned_data
        request.board.save(update_fields=["brief"])
    return redirect("board", token=token)


@require_POST
@with_board(parent_only=True)
def reset_family_link(request: HttpRequest, token: str) -> HttpResponse:
    services.reset_family_link(request.board)
    return HttpResponseClientRefresh()


@require_POST
@with_board(parent_only=True)
def delete_board(request: HttpRequest, token: str) -> HttpResponse:
    request.board.delete()
    return HttpResponseClientRedirect(reverse("home"))


@require_GET
@with_board(parent_only=True)
def catalog_shard(request: HttpRequest, token: str) -> JsonResponse:
    """The browser ranks these itself. The page puts the version in the URL, so this can be cached hard."""
    response = JsonResponse({"names": catalog.shard(request.board.brief)})
    response["Cache-Control"] = "private, max-age=86400"
    return response


@require_POST
@with_board(parent_only=True)
@ratelimit(group="catalog", key=_board_token, rate=settings.ADD_RATE_PER_BOARD, block=True)
def suggest_from_catalog(request: HttpRequest, token: str) -> HttpResponse:
    ids = [int(pk) for pk in request.POST.getlist("id") if pk.isdecimal()]
    names = services.add_from_catalog(request.board, ids)
    return render(request, "names/partials/review_cards.html", _context(request, queue=names))


def _ai_status(request: HttpRequest, job: Job) -> HttpResponse:
    """Parents keep swiping while AI works. This small element polls, then adds its cards to the deck."""
    if job.status == Job.Status.DONE:
        names = request.board.names.filter(pk__in=job.result.get("names", [])).defer("audio")
        where = "afterbegin" if job.payload.get("more_like") else "beforeend"
        context = _context(request, queue=names, finished=True, where=where)
    elif job.status == Job.Status.FAILED:
        context = _context(request, error=job.result.get("error"), finished=True)
    else:
        nudge(LLM)
        context = _context(request, job=job, finished=False)
    return render(request, "names/partials/ai_status.html", context)


@require_POST
@with_board(parent_only=True)
def suggest(request: HttpRequest, token: str) -> HttpResponse:
    _spend_ai_budget(request)
    return _ai_status(request, services.start_suggesting(request.board))


@require_GET
@with_board(parent_only=True)
def ai_job(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    return _ai_status(request, get_object_or_404(Job, pk=pk, board=request.board, kind="suggest"))


@require_GET
@with_board(parent_only=True)
def decisions(request: HttpRequest, token: str) -> JsonResponse:
    return JsonResponse(services.decisions(request.board, request.voter))


@require_GET
@with_board(parent_only=True)
def shortlist_panel(request: HttpRequest, token: str) -> HttpResponse:
    return _shortlist(request)


@require_GET
@with_board(parent_only=True)
def new_cards(request: HttpRequest, token: str) -> HttpResponse:
    ids = [int(pk) for pk in request.GET.getlist("id") if pk.isdecimal()]
    names = services.cards(request.board, request.voter, ids)
    return render(request, "names/partials/review_cards.html", _context(request, queue=names))


@require_POST
@with_board(parent_only=True)
def more_like_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    _spend_ai_budget(request)
    return _ai_status(request, services.start_suggesting(request.board, more_like=_name(request, pk).text))


@require_POST
@with_board(parent_only=True)
def keep_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    services.keep(_name(request, pk), request.voter)
    return _shortlist(request, oob=True)


@require_POST
@with_board(parent_only=True)
def pass_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    services.pass_on(_name(request, pk), request.voter)
    return _shortlist(request, oob=True)


@require_POST
@with_board(parent_only=True)
def undo_swipe(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    name = _name(request, pk)
    services.undo_swipe(name, request.voter)
    services.with_partner_notes([name], request.board, request.voter)
    context = _context(request, queue=[name], shortlist=services.shortlist(request.board, request.voter), oob=True)
    return render(request, "names/partials/undo.html", context)


@require_POST
@with_board(parent_only=True)
def bring_back(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    services.bring_back(_name(request, pk))
    return HttpResponseClientRefresh()


def _may_spend_ai(request: HttpRequest) -> bool:
    try:
        _spend_ai_budget(request)
    except Ratelimited:
        return False
    return True


def _may_refresh_family_ideas(request: HttpRequest) -> bool:
    if not accounts.family_refresh_allowed(request.board):
        return False
    rate = settings.FAMILY_REFRESH_RATE
    limited = is_ratelimited(request, group="family-refresh", key=_board_token, rate=rate, increment=True)
    return not limited and _may_spend_ai(request)


def _family_ideas_panel(request: HttpRequest, *, allow_llm: bool = False, no_new: bool = False) -> HttpResponse:
    board = request.board
    ideas = services.family_ideas(board, allow_llm=allow_llm)
    pending = services.family_ideas_pending(board)
    if pending:
        nudge(LLM)
    context = _context(
        request,
        ideas=ideas,
        pending=pending,
        no_new=no_new and not pending,
        can_refresh=not pending and accounts.family_refresh_allowed(board),
    )
    return render(request, "names/partials/family_ideas.html", context)


@require_POST
@with_board()
def family_ideas(request: HttpRequest, token: str) -> HttpResponse:
    board = request.board
    allow_llm = services.family_ideas_need_llm(board) and _may_spend_ai(request)
    no_new = "refreshing" in request.POST and services.family_refresh_failed(board)
    return _family_ideas_panel(request, allow_llm=allow_llm, no_new=no_new)


@require_POST
@with_board()
def refresh_family_ideas(request: HttpRequest, token: str) -> HttpResponse:
    board = request.board
    if services.family_ideas_pending(board):
        return _family_ideas_panel(request)
    if not _may_refresh_family_ideas(request):
        return _family_ideas_panel(request, no_new=True)
    services.refresh_family_ideas(board)
    return _family_ideas_panel(request, no_new=services.family_refresh_failed(board))


@require_POST
@with_board(parent_only=True)
def vote_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    services.toggle_vote(_name(request, pk), request.voter)
    return _shortlist(request)


@require_POST
@with_board(parent_only=True)
def rank(request: HttpRequest, token: str) -> HttpResponse:
    order = [int(pk) for pk in request.POST.getlist("order") if pk.isdecimal()]
    services.set_order(request.board, order)
    return HttpResponse(status=204)


@require_POST
@with_board()
@ratelimit(group="add", key=_board_token, rate=settings.ADD_RATE_PER_BOARD, block=True)
@ratelimit(group="add-ip", key=_client_ip, rate=settings.ADD_RATE_PER_IP, block=True)
def add(request: HttpRequest, token: str) -> HttpResponse:
    form = AddNameForm(request.POST)
    if not form.is_valid():
        return HttpResponse(NOT_A_NAME, status=422, content_type="text/plain")
    _admit_guest(request)
    if form.cleaned_data["by"]:
        request.session["voter_name"] = form.cleaned_data["by"]
    text = form.cleaned_data["text"]
    voter = _voter(request, request.is_parent)
    result = services.add_name(request.board, text, voter, force="force" in request.POST)
    if result.duplicate:
        context = _context(request, match=result.duplicate, text=text)
        return render(request, "names/partials/duplicate_prompt.html", context)
    return _added(request, result.name, text)


def _added(request: HttpRequest, name: Name | None, text: str) -> HttpResponse:
    context = _context(
        request,
        name=name,
        shortlist=services.shortlist(request.board, request.voter),
        oob=True,
        message=f"{text} added." if request.is_parent else f"Thanks. {text} is with the parents now.",
    )
    return render(request, "names/partials/added.html", context)


@require_POST
@with_board(parent_only=True)
def merge_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    name = _name(request, pk)
    form = AddNameForm(request.POST)
    if not form.is_valid():
        return HttpResponse(NOT_A_NAME, status=422, content_type="text/plain")
    if not services.merge_into(name, form.cleaned_data["text"], request.voter):
        return HttpResponse(DIFFERENT_NAME, status=422, content_type="text/plain")
    context = _context(
        request,
        name=None,
        shortlist=services.shortlist(request.board, request.voter),
        oob=True,
        message=f"Your vote is on {name.text}.",
    )
    return render(request, "names/partials/added.html", context)


@require_POST
@with_board(parent_only=True)
def official_spelling(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    services.make_official(_name(request, pk), request.POST.get("variant", ""))
    return _shortlist(request)


@require_POST
@with_board(parent_only=True)
def split_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    services.split_variant(_name(request, pk), request.POST.get("variant", ""), request.voter)
    return _shortlist(request)


@require_GET
@with_board(parent_only=True)
def name_number(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    name = _name(request, pk)
    context = _context(request, name=name, stop_asking=not services.request_name_number(name))
    return render(request, "names/partials/name_number.html", context)


@require_POST
@with_board(parent_only=True)
def name_details(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    name = _name(request, pk)
    state = services.describe_name(name)
    context = _context(request, name=name, no_details=state == services.Details.UNAVAILABLE)
    return render(request, "names/partials/name_details.html", context)
