from django.urls import path

from . import account_views, views

name_actions = {
    "keep": views.keep_name,
    "pass": views.pass_name,
    "undo": views.undo_swipe,
    "restore": views.bring_back,
    "more": views.more_like_name,
    "vote": views.vote_name,
    "merge": views.merge_name,
    "official": views.official_spelling,
    "split": views.split_name,
    "audio": views.name_audio,
    "details": views.name_details,
}

urlpatterns = [
    path("", views.home, name="home"),
    path("legal/privacy", account_views.legal_page("privacy"), name="privacy"),
    path("legal/terms", account_views.legal_page("terms"), name="terms"),
    path("legal/cookies", account_views.legal_page("cookies"), name="cookies"),
    path("legal/disclaimer", account_views.legal_page("disclaimer"), name="disclaimer"),
    path("legal/grievance", account_views.legal_page("grievance"), name="grievance"),
    path("legal/accept", account_views.accept_policies, name="accept_policies"),
    path("me/", account_views.dashboard, name="dashboard"),
    path("me/settings", account_views.account_settings, name="account_settings"),
    path("me/export", account_views.export, name="account_export"),
    path("me/delete", account_views.delete, name="account_delete"),
    path("me/contribute", account_views.contribute, name="contribute"),
    path("join/<str:token>/", views.join, name="join"),
    path("b/<str:token>/", views.board_page, name="board"),
    path("b/<str:token>/family-link/reset", views.reset_family_link, name="reset_family_link"),
    path("b/<str:token>/delete", views.delete_board, name="delete_board"),
    path("b/<str:token>/brief", views.update_brief, name="brief"),
    path("b/<str:token>/suggest", views.suggest, name="suggest"),
    path("b/<str:token>/catalog.json", views.catalog_shard, name="catalog_shard"),
    path("b/<str:token>/suggest/catalog", views.suggest_from_catalog, name="suggest_from_catalog"),
    path("b/<str:token>/jobs/<int:pk>", views.ai_job, name="ai_job"),
    path("b/<str:token>/decisions.json", views.decisions, name="decisions"),
    path("b/<str:token>/shortlist", views.shortlist_panel, name="shortlist_panel"),
    path("b/<str:token>/cards", views.new_cards, name="new_cards"),
    path("b/<str:token>/ideas", views.family_ideas, name="family_ideas"),
    path("b/<str:token>/rank", views.rank, name="rank"),
    path("b/<str:token>/names", views.add, name="add"),
    *[path(f"b/<str:token>/names/<int:pk>/{slug}", view, name=slug) for slug, view in name_actions.items()],
]
