"""The operator's panel. Family boards are deliberately absent: staff run the service, they don't read private lists."""

from django.contrib import admin, messages
from django.db.models import Count, Sum
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone

from . import catalog
from .dedup import phonetic_key
from .forms import CatalogUploadForm
from .models import AICall, Board, CatalogName, ConsentRecord, Genre, Job, Name, NameAlias

admin.site.site_header = "Naamadeva admin"
admin.site.site_title = "Naamadeva admin"
admin.site.index_title = "Overview"
admin.site.index_template = "admin/names_index.html"

_index = admin.site.index


def overview() -> dict:
    month = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    served = Name.objects.filter(created_at__gte=month)
    from_catalog = served.filter(source=Name.Source.CATALOG).count()
    from_llm = served.filter(source=Name.Source.LLM).count()
    return {
        "boards": Board.objects.count(),
        "pending": CatalogName.objects.filter(status=CatalogName.Status.PENDING).count(),
        "approved": CatalogName.objects.filter(status=CatalogName.Status.APPROVED).count(),
        "llm_calls": AICall.objects.filter(created_at__gte=month).count(),
        "llm_by_kind": AICall.objects.filter(created_at__gte=month)
        .values("kind")
        .annotate(total=Count("pk"), names=Sum("units")),
        "insights_cached": CatalogName.objects.filter(enrich_state=CatalogName.Enrich.DONE).count(),
        "voices_ready": CatalogName.objects.filter(audio__isnull=False).count(),
        "voices_waiting": CatalogName.objects.filter(voice_state=CatalogName.Enrich.PENDING).count(),
        "aliases": NameAlias.objects.count(),
        "jobs_waiting": Job.objects.filter(status__in=[Job.Status.QUEUED, Job.Status.RUNNING]).count(),
        "jobs_failed": Job.objects.filter(status=Job.Status.FAILED).count(),
        "catalog_share": round(100 * from_catalog / (from_catalog + from_llm)) if from_catalog + from_llm else None,
    }


def _admin_index(request, extra_context=None):
    return _index(request, {**(extra_context or {}), "overview": overview()})


admin.site.index = _admin_index


class ReadOnlyAdmin(admin.ModelAdmin):
    """Audit trails: changing them by hand would make them worthless."""

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ["name", "active", "name_count"]
    list_editable = ["active"]
    search_fields = ["name"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(total=Count("names"))

    @admin.display(description="Names", ordering="total")
    def name_count(self, genre) -> int:
        return genre.total


class NameAliasInline(admin.TabularInline):
    model = NameAlias
    extra = 0


@admin.register(CatalogName)
class CatalogNameAdmin(admin.ModelAdmin):
    inlines = [NameAliasInline]
    change_list_template = "admin/names/catalogname/change_list.html"
    list_display = ["text", "native", "origin", "gender", "rarity", "status", "source", "submissions", "picks"]
    list_filter = ["status", "source", "rarity", "gender", "origin", "genres"]
    search_fields = ["text", "native", "meaning", "key"]
    filter_horizontal = ["genres"]
    readonly_fields = [
        "key", "source", "submitted_by", "submissions", "picks", "updated_at", "enrich_state", "voice_state",
    ]  # fmt: skip
    exclude = ["enrich_attempts", "enrich_claimed_at", "voice_attempts", "voice_claimed_at"]
    actions = ["approve", "reject"]
    ordering = ["-submissions", "text"]

    def get_queryset(self, request):
        return super().get_queryset(request).defer("audio")

    @admin.action(description="Approve selected names")
    def approve(self, request, queryset) -> None:
        self.message_user(request, f"{queryset.update(status=CatalogName.Status.APPROVED)} names approved.")
        catalog.queue_blank_entries(queryset)

    @admin.action(description="Reject selected names")
    def reject(self, request, queryset) -> None:
        self.message_user(request, f"{queryset.update(status=CatalogName.Status.REJECTED)} names rejected.")

    def save_model(self, request, obj, form, change) -> None:
        if not obj.key:
            obj.key = phonetic_key(obj.text)[:60]
        super().save_model(request, obj, form, change)

    def get_urls(self):
        upload = self.admin_site.admin_view(self.upload_view)
        return [path("upload/", upload, name="names_catalogname_upload"), *super().get_urls()]

    def upload_view(self, request):
        if not self.has_add_permission(request):
            return redirect("admin:index")
        form = CatalogUploadForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            self._import(request, form.cleaned_data)
            return redirect(reverse("admin:names_catalogname_changelist"))
        context = {
            **self.admin_site.each_context(request),
            "form": form,
            "title": "Upload names",
            "columns": catalog.COLUMNS,
        }
        return render(request, "admin/names/catalogname/upload.html", context)

    def _import(self, request, data: dict) -> None:
        status = CatalogName.Status.APPROVED if data["publish"] else CatalogName.Status.PENDING
        options = catalog.ImportOptions(
            source=CatalogName.Source.ADMIN,
            status=status,
            genres=tuple(data["genres"]),
            submitted_by=request.user,
            row_limit=50000,
        )
        result = catalog.import_rows(data["file"], options)
        catalog.queue_blank_entries(CatalogName.objects.filter(status=CatalogName.Status.APPROVED))
        self.message_user(request, f"{result.created} added, {result.merged} merged into existing names.")
        for error in result.errors[:20]:
            self.message_user(request, error, messages.WARNING)


@admin.register(Job)
class JobAdmin(ReadOnlyAdmin):
    """Without payloads, which can hold email addresses, sign-in codes and birth details."""

    fields = list_display = ["created_at", "queue", "kind", "status", "attempts", "updated_at"]
    list_filter = ["queue", "kind", "status"]


@admin.register(ConsentRecord)
class ConsentRecordAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "user", "purpose", "action", "policy_version"]
    list_filter = ["purpose", "action", "policy_version"]


@admin.register(AICall)
class AICallAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "kind", "user"]
    list_filter = ["kind"]
    date_hierarchy = "created_at"


@admin.register(Board)
class BoardAdmin(ReadOnlyAdmin):
    """Counts only. No brief, names or tokens: seeing them here would let staff open any family's board."""

    fields = list_display = ["id", "created_at", "member_count", "name_count"]

    def has_delete_permission(self, request, obj=None) -> bool:
        return True  # Erasure requests that come in through the grievance address.

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(people=Count("members", distinct=True), total=Count("names"))

    @admin.display(description="Parents", ordering="people")
    def member_count(self, board) -> int:
        return board.people

    @admin.display(description="Names", ordering="total")
    def name_count(self, board) -> int:
        return board.total
