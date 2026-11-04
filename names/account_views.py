"""The signed-in person's own space, and the legal pages everyone can read."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from . import accounts, catalog
from .analytics import allow_web_analytics
from .forms import ContributeForm, DeleteAccountForm, ProfileForm
from .models import CatalogName, ConsentRecord, Genre


def legal_page(page: str):
    @allow_web_analytics
    @require_GET
    def view(request: HttpRequest) -> HttpResponse:
        return render(request, f"names/legal/{page}.html")

    return view


@require_http_methods(["GET", "POST"])
@login_required
def accept_policies(request: HttpRequest) -> HttpResponse:
    target = request.POST.get("next") or request.GET.get("next", "")
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        target = "dashboard"
    if request.method == "POST" and "accept" in request.POST:
        accounts.record_consent(request.user)
        return redirect(target)
    return render(request, "names/legal/accept.html", {"next": target})


@require_GET
@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    boards = request.user.boards.order_by("-created_at").prefetch_related("names")
    context = {
        "boards": boards,
        "ai_left": accounts.ai_calls_left(request.user),
        "ai_total": settings.AI_FREE_CALLS_PER_MONTH,
        "profile": accounts.profile_of(request.user),
        "contributions": CatalogName.objects.filter(submitted_by=request.user).count(),
    }
    return render(request, "names/account/dashboard.html", context)


@require_http_methods(["GET", "POST"])
@login_required
def account_settings(request: HttpRequest) -> HttpResponse:
    form = ProfileForm(request.POST or None, instance=accounts.profile_of(request.user))
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect("account_settings")
    context = {"form": form, "delete_form": DeleteAccountForm(), "consents": request.user.consents.all()[:20]}
    return render(request, "names/account/settings.html", context)


@require_GET
@login_required
@ratelimit(group="export", key="user", rate="10/h", block=True)
def export(request: HttpRequest) -> JsonResponse:
    response = JsonResponse(accounts.export_data(request.user), json_dumps_params={"ensure_ascii": False, "indent": 2})
    response["Content-Disposition"] = 'attachment; filename="naamadeva-my-data.json"'
    return response


@require_POST
@login_required
def delete(request: HttpRequest) -> HttpResponse:
    form = DeleteAccountForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Your account is still here. Type DELETE, in capitals, to confirm.")
        return redirect("account_settings")
    user = request.user
    logout(request)
    accounts.delete_account(user)
    messages.success(request, "Your account and data are deleted.")
    return redirect("home")


def _contribute(request: HttpRequest, form: ContributeForm) -> catalog.ImportResult:
    waiting = CatalogName.objects.filter(submitted_by=request.user, status=CatalogName.Status.PENDING).count()
    room = settings.MAX_PENDING_CONTRIBUTIONS - waiting
    if room <= 0:
        return catalog.ImportResult(errors=["Thanks! Many of your names are waiting for review. Add more afterwards."])
    data = form.cleaned_data
    options = catalog.ImportOptions(
        source=CatalogName.Source.USER,
        status=CatalogName.Status.PENDING,
        genres=tuple(Genre.objects.filter(name__in=data["genres"])),
        origin=data["origin"],
        gender=data["gender"] or "any",
        submitted_by=request.user,
        row_limit=min(catalog.ImportOptions.row_limit, room),
    )
    accounts.record_consent(request.user, ConsentRecord.Purpose.CONTRIBUTION)
    return catalog.import_rows(data["names"], options)


@require_http_methods(["GET", "POST"])
@login_required
@ratelimit(group="contribute", key="user", rate="10/h", method="POST", block=True)
def contribute(request: HttpRequest) -> HttpResponse:
    form = ContributeForm(request.POST or None, request.FILES or None)
    result = _contribute(request, form) if request.method == "POST" and form.is_valid() else None
    mine = CatalogName.objects.filter(submitted_by=request.user).order_by("-updated_at")[:30]
    return render(request, "names/account/contribute.html", {"form": form, "result": result, "mine": mine})
