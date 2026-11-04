import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from names.models import Board, Name

pytestmark = pytest.mark.django_db
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


@pytest.fixture
def board():
    return Board.objects.create(brief={"surname": "Sharma"})


@pytest.fixture
def parent(board):
    return Client(), board.parent_token


@pytest.fixture
def guest(board):
    return Client(), board.guest_token


def post(actor, route, *args, **data):
    client, token = actor
    return client.post(reverse(route, args=[token, *args]), data, **HTMX)


def page(actor):
    client, token = actor
    return client.get(reverse("board", args=[token]))


def test_brief_creates_a_board_and_redirects_to_it():
    response = Client().post(reverse("home"), {"surname": "Rao", "themes": ["Light"], "origins": ["Tamil"]})
    board = Board.objects.get()
    assert response.status_code == 302
    assert board.parent_token in response.url
    assert board.brief["themes"] == ["Light"]


def test_suggestions_show_meaning_pronunciation_and_surname(parent, board):
    response = post(parent, "suggest")
    assert response.status_code == 200
    assert "AA-ruhv" in response.content.decode()
    assert "Sharma" in response.content.decode()
    assert board.names.count() == 4


def test_suggesting_again_never_repeats_a_name(parent, board):
    post(parent, "suggest")
    post(parent, "suggest")
    keys = list(board.names.values_list("key", flat=True))
    assert len(keys) == len(set(keys))


def test_keep_moves_a_name_to_the_shortlist_and_pass_hides_it(parent, guest, board):
    post(parent, "suggest")
    kept, passed = board.names.all()[:2]
    post(parent, "keep", kept.pk)
    post(parent, "pass", passed.pk)
    html = page(guest).content.decode()
    assert kept.text in html
    assert passed.text not in html


def test_guest_duplicate_asks_before_merging(guest, board):
    existing = Name.objects.create(board=board, text="Aarav", key="arav", status=Name.Status.KEPT)
    response = post(guest, "add", text="आरव")
    assert "the same name as" in response.content.decode()
    assert board.names.count() == 1

    post(guest, "merge", existing.pk, text="आरव")
    existing.refresh_from_db()
    assert existing.variants == ["आरव"]
    assert existing.votes.count() == 1


def test_guest_can_insist_it_is_a_different_name(guest, board):
    Name.objects.create(board=board, text="Aarav", key="arav")
    post(guest, "add", text="Arav", force="1")
    assert board.names.count() == 2


def test_guest_suggestion_waits_for_parents(guest, parent, board):
    post(guest, "add", text="Zoya", by="Maa")
    name = board.names.get()
    assert name.status == Name.Status.REVIEW
    assert name.suggested_by == "Maa"
    assert "Zoya" in page(parent).content.decode()
    assert "Zoya" not in page(guest).content.decode()


def test_two_parents_voting_are_recognised(board):
    first, second = (Client(), board.parent_token), (Client(), board.parent_token)
    name = Name.objects.create(board=board, text="Tara", key="tara", status=Name.Status.KEPT)
    post(first, "vote", name.pk)
    post(second, "vote", name.pk)
    assert "You both love this" in page(first).content.decode()


def test_vote_toggles(guest, board):
    name = Name.objects.create(board=board, text="Tara", key="tara", status=Name.Status.KEPT)
    post(guest, "vote", name.pk)
    post(guest, "vote", name.pk)
    assert name.votes.count() == 0


def test_undoing_a_merge_makes_a_separate_name(parent, board):
    name = Name.objects.create(board=board, text="Aarav", key="arav", variants=["Arnav"], status=Name.Status.KEPT)
    post(parent, "split", name.pk, variant="Arnav")
    name.refresh_from_db()
    assert name.variants == []
    assert board.names.filter(text="Arnav").exists()


def test_choosing_the_official_spelling_swaps_it(parent, board):
    name = Name.objects.create(board=board, text="Aaravv", key="arav", variants=["Aarav"], status=Name.Status.KEPT)
    post(parent, "official", name.pk, variant="Aarav")
    name.refresh_from_db()
    assert (name.text, name.variants) == ("Aarav", ["Aaravv"])


def test_ranking_saves_the_new_order(parent, board):
    a = Name.objects.create(board=board, text="A", key="a", status=Name.Status.KEPT, rank=0)
    b = Name.objects.create(board=board, text="B", key="b", status=Name.Status.KEPT, rank=1)
    post(parent, "rank", order=[b.pk, a.pk])
    assert list(board.names.values_list("text", flat=True)) == ["B", "A"]


def test_audio_is_generated_once_then_served_from_the_database(guest, board):
    name = Name.objects.create(board=board, text="Tara", key="tara")
    client, token = guest
    response = client.get(reverse("audio", args=[token, name.pk]))
    assert response["Content-Type"] == "audio/wav"
    name.refresh_from_db()
    assert name.audio


@pytest.mark.parametrize(
    ("route", "args"), [("suggest", ()), ("rank", ()), ("brief", ()), ("keep", (1,)), ("pass", (1,)), ("split", (1,))]
)
def test_guest_link_cannot_use_parent_actions(guest, route, args):
    assert post(guest, route, *args).status_code == 404


def test_family_link_never_reveals_the_parent_link(guest, board):
    assert board.parent_token not in page(guest).content.decode()


def test_names_on_another_board_are_out_of_reach(parent):
    other = Board.objects.create()
    name = Name.objects.create(board=other, text="Tara", key="tara")
    assert post(parent, "keep", name.pk).status_code == 404


def test_unknown_link_is_not_found():
    assert Client().get(reverse("board", args=["nope"])).status_code == 404


def test_guest_input_is_escaped_and_validated(guest, parent, board):
    assert post(guest, "add", text="<script>alert(1)</script>").status_code == 422
    post(guest, "add", text="Asha", by="<b>x</b>")
    assert "<b>x</b>" not in page(parent).content.decode()


@override_settings(AI_BACKEND="names.ai.gemini.GeminiAI", GEMINI_API_KEY="")
def test_missing_key_gives_a_clear_error_and_never_leaks_config(parent):
    from names.ai.base import get_ai

    get_ai.cache_clear()
    response = post(parent, "suggest")
    get_ai.cache_clear()
    assert response.status_code == 502
    assert "GEMINI_API_KEY" in response.content.decode()


@override_settings(AI_BACKEND="names.ai.gemini.GeminiAI", GEMINI_API_KEY="secret-value-123")
def test_api_key_never_reaches_a_page(parent):
    assert "secret-value-123" not in page(parent).content.decode()


def test_ai_budget_blocks_a_flood_on_one_board(parent):
    statuses = {post(parent, "suggest").status_code for _ in range(31)}
    assert 429 in statuses


def test_security_headers_are_set(parent):
    response = page(parent)
    assert "script-src" in response["Content-Security-Policy"]
    assert response["Referrer-Policy"] == "same-origin"


def test_healthcheck():
    assert Client().get("/healthz").content == b"ok"
