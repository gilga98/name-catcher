from django.urls import path

from . import views

name_actions = {
    "keep": views.keep_name,
    "pass": views.pass_name,
    "more": views.more_like_name,
    "vote": views.vote_name,
    "merge": views.merge_name,
    "official": views.official_spelling,
    "split": views.split_name,
    "audio": views.name_audio,
}

urlpatterns = [
    path("", views.home, name="home"),
    path("join/<str:token>/", views.join, name="join"),
    path("b/<str:token>/", views.board_page, name="board"),
    path("b/<str:token>/family-link/reset", views.reset_family_link, name="reset_family_link"),
    path("b/<str:token>/delete", views.delete_board, name="delete_board"),
    path("b/<str:token>/brief", views.update_brief, name="brief"),
    path("b/<str:token>/suggest", views.suggest, name="suggest"),
    path("b/<str:token>/rank", views.rank, name="rank"),
    path("b/<str:token>/names", views.add, name="add"),
    *[path(f"b/<str:token>/names/<int:pk>/{slug}", view, name=slug) for slug, view in name_actions.items()],
]
