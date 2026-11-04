import uuid
from functools import wraps

from django.conf import settings
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from . import services
from .forms import AddNameForm, BriefForm
from .models import Board, Name
from .services import Voter


def rate_limited(request: HttpRequest, exception: Exception) -> HttpResponse:
    message = "That's a lot of requests for now. Try again in a little while."
    return HttpResponse(message, status=429, content_type="text/plain")


def _ai_budget(view):
    """Cap LLM spend per board and overall, so a leaked link can't run up the bill."""
    per_board = ratelimit(
        group="ai-board",
        key=lambda _group, request: request.resolver_match.kwargs["token"],
        rate=settings.AI_RATE_PER_BOARD,
        block=True,
    )
    overall = ratelimit(group="ai-all", key=lambda _group, _request: "all", rate=settings.AI_RATE_GLOBAL, block=True)
    return overall(per_board(view))


def _voter(request: HttpRequest, is_parent: bool) -> Voter:
    voter_id = request.session.setdefault("voter", uuid.uuid4().hex)
    default = "Parent" if is_parent else "Family"
    return Voter(id=voter_id, name=request.session.get("voter_name", default), is_parent=is_parent)


def with_board(*, parent_only: bool = False):
    """Resolve the link in the URL to its board. The token is the only credential."""

    def decorator(view):
        @wraps(view)
        def wrapper(request: HttpRequest, token: str, *args, **kwargs):
            board = Board.objects.filter(Q(parent_token=token) | Q(guest_token=token)).first()
            is_parent = board is not None and board.parent_token == token
            if board is None or (parent_only and not is_parent):
                raise Http404
            request.board, request.is_parent = board, is_parent
            request.voter = _voter(request, is_parent)
            return view(request, token, *args, **kwargs)

        return wrapper

    return decorator


def _name(request: HttpRequest, pk: int) -> Name:
    return get_object_or_404(request.board.names.defer("audio"), pk=pk)


def _context(request: HttpRequest, **extra) -> dict:
    return {
        "board": request.board,
        "token": request.resolver_match.kwargs["token"],
        "is_parent": request.is_parent,
        "surname": request.board.brief.get("surname", ""),
        **extra,
    }


def _shortlist(request: HttpRequest, *, oob: bool = False, template: str = "shortlist") -> HttpResponse:
    names = services.shortlist(request.board, request.voter.id)
    return render(request, f"names/partials/{template}.html", _context(request, shortlist=names, oob=oob))


@require_http_methods(["GET", "POST"])
@ratelimit(group="create", key=lambda _g, _r: "all", rate=settings.BOARD_CREATE_RATE, method="POST", block=True)
def home(request: HttpRequest) -> HttpResponse:
    form = BriefForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        board = Board.objects.create(brief=form.cleaned_data)
        return redirect("board", token=board.parent_token)
    return render(request, "names/home.html", {"form": form})


@require_GET
@with_board()
def board_page(request: HttpRequest, token: str) -> HttpResponse:
    board = request.board
    queue = board.names.filter(status=Name.Status.REVIEW).defer("audio") if request.is_parent else []
    guest_url = request.build_absolute_uri(reverse("board", args=[board.guest_token]))
    context = _context(
        request,
        queue=queue,
        shortlist=services.shortlist(board, request.voter.id),
        is_new=request.is_parent and not board.names.exists(),
        guest_url=guest_url,
        brief_form=BriefForm(initial=board.brief),
        by=request.session.get("voter_name", ""),
    )
    return render(request, "names/board.html", context)


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
@_ai_budget
def suggest(request: HttpRequest, token: str) -> HttpResponse:
    names = services.suggest_names(request.board)
    return render(request, "names/partials/review_cards.html", _context(request, queue=names))


@require_POST
@with_board(parent_only=True)
@_ai_budget
def more_like_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    names = services.suggest_names(request.board, more_like=_name(request, pk).text)
    return render(request, "names/partials/review_cards.html", _context(request, queue=names))


@require_POST
@with_board(parent_only=True)
def keep_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    services.keep(_name(request, pk), request.voter)
    return _shortlist(request, oob=True)


@require_POST
@with_board(parent_only=True)
def pass_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    services.pass_on(_name(request, pk))
    return HttpResponse("")


@require_POST
@with_board()
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
@_ai_budget
def add(request: HttpRequest, token: str) -> HttpResponse:
    form = AddNameForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Enter just the name, up to 40 letters.", status=422, content_type="text/plain")
    if form.cleaned_data["by"]:
        request.session["voter_name"] = form.cleaned_data["by"]
    text = form.cleaned_data["text"]
    result = services.add_name(
        request.board, text, _voter(request, request.is_parent), force="force" in request.POST
    )
    if result.duplicate:
        context = _context(request, match=result.duplicate, text=text)
        return render(request, "names/partials/duplicate_prompt.html", context)
    return _added(request, result.name)


def _added(request: HttpRequest, name: Name) -> HttpResponse:
    context = _context(
        request,
        name=name,
        shortlist=services.shortlist(request.board, request.voter.id),
        oob=True,
        message=f"{name.text} added." if request.is_parent else f"Thanks. {name.text} is with the parents now.",
    )
    return render(request, "names/partials/added.html", context)


@require_POST
@with_board()
def merge_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    name = _name(request, pk)
    form = AddNameForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Enter just the name, up to 40 letters.", status=422, content_type="text/plain")
    services.merge_into(name, form.cleaned_data["text"], request.voter)
    context = _context(
        request,
        name=None,
        shortlist=services.shortlist(request.board, request.voter.id),
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
@_ai_budget
def split_name(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    services.split_variant(_name(request, pk), request.POST.get("variant", ""), request.voter)
    return _shortlist(request)


@require_GET
@with_board()
@_ai_budget
def name_audio(request: HttpRequest, token: str, pk: int) -> HttpResponse:
    audio = services.pronunciation_audio(get_object_or_404(request.board.names, pk=pk))
    response = HttpResponse(audio, content_type="audio/wav")
    response["Cache-Control"] = "private, max-age=86400"
    return response
