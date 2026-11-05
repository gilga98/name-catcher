import re

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from names import services
from names.accounts import record_consent
from names.models import Board, CatalogName, Name

pytestmark = pytest.mark.django_db
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


@pytest.fixture
def board():
    return Board.objects.create(brief={"surname": "Sharma"})


def member(board, email="parent@example.com"):
    user = get_user_model().objects.create_user(username=email, email=email, password="not-a-real-pw-1")
    board.members.add(user)
    client = Client()
    record_consent(user)
    client.force_login(user)
    return client, board.parent_token


@pytest.fixture
def parent(board):
    return member(board)


@pytest.fixture
def guest(board):
    return Client(), board.guest_token


def post(actor, route, *args, **data):
    client, token = actor
    return client.post(reverse(route, args=[token, *args]), data, **HTMX)


def page(actor):
    client, token = actor
    return client.get(reverse("board", args=[token]))


def test_new_parent_swipes_immediately_then_claims_board():
    client = Client()
    response = client.post(reverse("home"), {"surname": "Rao", "notes": "Something bright, like the sunrise"})
    board = Board.objects.get()
    assert response.url == reverse("board", args=[board.parent_token])
    assert board.brief["themes"] == ["Light"]
    assert not board.members.exists()

    # The provisional parent can view and use the board immediately:
    board_page = client.get(reverse("board", args=[board.parent_token]))
    assert board_page.status_code == 200

    # Next, they create an account to save their board:
    client.post(
        reverse("account_signup"),
        {
            "email": "new@example.com",
            "password1": "a-long-passphrase-9",
            "accept_terms": "on",
            "confirm_adult": "on",
        },
    )

    board.refresh_from_db()
    assert board.members.get().email == "new@example.com"


def test_provisional_swipes_and_votes_migrate_to_claimed_account():
    client = Client()
    client.post(reverse("home"), {"surname": "Patel"})
    board = Board.objects.get()
    name = Name.objects.create(board=board, text="Aarav", key="aarav")

    # Unauthenticated swipe keep
    client.post(reverse("keep", args=[board.parent_token, name.pk]), **HTMX)
    name.refresh_from_db()
    assert name.status == Name.Status.KEPT
    assert name.votes.count() == 1

    # Now sign up
    client.post(
        reverse("account_signup"),
        {
            "email": "aarav-parent@example.com",
            "password1": "a-long-passphrase-9",
            "accept_terms": "on",
            "confirm_adult": "on",
        },
    )
    user = get_user_model().objects.get(email="aarav-parent@example.com")
    name.refresh_from_db()
    vote = name.votes.get()
    assert vote.voter == f"user-{user.pk}"
    swipe = name.swipes.get()
    assert swipe.voter == f"user-{user.pk}"


def test_unauthenticated_partner_sees_invite_page_before_login(board):
    client = Client()
    response = client.get(reverse("join", args=[board.invite_token]))
    assert response.status_code == 200
    assert b"Sharma" in response.content


def test_signed_in_parent_starts_a_board_straight_away(board, parent):
    client, _ = parent
    response = client.post(reverse("home"), {"surname": "Rao"})
    assert Board.objects.count() == 2
    assert Board.objects.exclude(pk=board.pk).get().parent_token in response.url


def test_home_lists_only_my_boards(board, parent):
    other = Board.objects.create(brief={"surname": "Stranger"})
    html = parent[0].get(reverse("home")).content.decode()
    assert board.parent_token in html
    assert other.parent_token not in html


def test_parent_page_requires_sign_in(board):
    response = Client().get(reverse("board", args=[board.parent_token]))
    assert response.status_code == 302
    assert reverse("account_login") in response.url


def test_expired_session_on_an_htmx_action_goes_to_login(board):
    response = Client().post(reverse("suggest", args=[board.parent_token]), **HTMX)
    assert reverse("account_login") in response["HX-Redirect"]
    assert not board.names.exists()


def test_signed_in_stranger_cannot_see_a_board(board):
    stranger = Board.objects.create()
    client, _ = member(stranger, "stranger@example.com")
    assert client.get(reverse("board", args=[board.parent_token])).status_code == 404


def test_partner_joins_with_an_invite_that_works_once(board, parent):
    stranger = Board.objects.create()
    client, _ = member(stranger, "partner@example.com")
    url = reverse("join", args=[board.invite_token])
    assert client.get(url).status_code == 200
    assert not board.members.filter(email="partner@example.com").exists()
    client.post(url)
    assert board.members.filter(email="partner@example.com").exists()
    assert client.post(url).status_code == 404


def test_invite_requires_sign_in(board):
    response = Client().post(reverse("join", args=[board.invite_token]))
    assert reverse("account_login") in response.url
    assert board.members.count() == 0


def test_family_link_can_be_replaced(parent, guest, board):
    old_guest = guest
    post(parent, "reset_family_link")
    assert page(old_guest).status_code == 404
    board.refresh_from_db()
    assert page((Client(), board.guest_token)).status_code == 200


def test_deleting_a_board_removes_everything(parent, board):
    Name.objects.create(board=board, text="Tara", key="tara")
    post(parent, "delete_board")
    assert not Board.objects.exists()
    assert not Name.objects.exists()


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
    assert kept.text in page(parent).content.decode()
    html = page(guest).content.decode()
    assert kept.text not in html
    assert passed.text not in html


def test_parent_duplicate_asks_before_merging(parent, board):
    existing = Name.objects.create(board=board, text="Aarav", key="arav", status=Name.Status.KEPT)
    response = post(parent, "add", text="आरव")
    assert "the same name as" in response.content.decode()
    assert board.names.count() == 1

    post(parent, "merge", existing.pk, text="आरव")
    existing.refresh_from_db()
    assert existing.variants == ["आरव"]
    assert existing.votes.count() == 1


def test_guest_duplicate_joins_without_asking(guest, board):
    existing = Name.objects.create(board=board, text="Aarav", key="arav", status=Name.Status.KEPT)
    response = post(guest, "add", text="आरव")
    assert "the same name as" not in response.content.decode()
    assert board.names.count() == 1
    existing.refresh_from_db()
    assert existing.variants == ["आरव"]
    assert existing.votes.count() == 1


def test_relatives_sending_the_same_idea_share_one_card(guest, board):
    post(guest, "add", text="Vihaan", by="Maa")
    post((Client(), board.guest_token), "add", text="Vihaan", by="Papa")
    name = board.names.get()
    assert name.suggested_by == "Maa"
    assert [vote.voter_name for vote in name.votes.all()] == ["Papa"]


def test_a_relative_cannot_insist_on_a_second_copy(guest, board):
    Name.objects.create(board=board, text="Aarav", key="arav")
    post(guest, "add", text="Arav", force="1")
    assert board.names.count() == 1


def test_a_parent_can_still_say_it_is_a_different_name(parent, board):
    Name.objects.create(board=board, text="Aarav", key="arav")
    post(parent, "add", text="Arav", force="1")
    assert board.names.count() == 2


class RecordingAI:
    def __init__(self):
        self.requests = []

    def suggest(self, request):
        self.requests.append(request)
        return []


def test_the_llm_is_told_what_waits_in_the_deck(board, monkeypatch):
    ai = RecordingAI()
    monkeypatch.setattr("names.services.get_ai", lambda: ai)
    Name.objects.create(board=board, text="Rohan", key="rohan", source=Name.Source.CATALOG)
    Name.objects.create(board=board, text="Zoya", key="zoya", suggested_by="Maa")
    services.suggest_names(board)
    assert ai.requests[0].shown == ["Rohan"]
    assert ai.requests[0].family_suggested == ["Zoya"]


def test_the_first_family_ask_rules_out_ideas_already_on_screen(board, monkeypatch):
    ai = RecordingAI()
    monkeypatch.setattr("names.services.get_ai", lambda: ai)
    CatalogName.objects.create(text="Tara", key="tara", meaning="Star", status=CatalogName.Status.APPROVED)
    services.fetch_family_ideas(board)
    assert ai.requests[0].shown == ["Tara"]
    assert "Tara" in [idea["text"] for idea in services.family_ideas(board, allow_llm=False)]


@override_settings(JOBS_EAGER=False)
def test_adding_a_name_never_waits_on_the_llm(parent, guest, board, monkeypatch):
    def no_llm():
        raise AssertionError("adding a name must not call the LLM")

    monkeypatch.setattr("names.services.get_ai", no_llm)
    assert post(parent, "add", text="Zoya").status_code == 200
    assert post(guest, "add", text="Meera", by="Maa").status_code == 200
    assert post(guest, "add", text="Mira").status_code == 200
    assert board.names.count() == 2


def test_details_load_after_adding_and_only_once(parent, board, monkeypatch):
    with override_settings(JOBS_EAGER=False):
        post(parent, "add", text="Zoya")
        name = board.names.get()
        assert reverse("details", args=[parent[1], name.pk]) in page(parent).content.decode()

    response = post(parent, "details", name.pk)
    name.refresh_from_db()
    assert name.meaning in response.content.decode()
    assert reverse("details", args=[parent[1], name.pk]) not in page(parent).content.decode()

    monkeypatch.setattr("names.services.get_ai", lambda: pytest.fail("details were already known"))
    assert post(parent, "details", name.pk).status_code == 200


def test_guest_suggestion_waits_for_parents(guest, parent, board):
    post(guest, "add", text="Zoya", by="Maa")
    name = board.names.get()
    assert name.status == Name.Status.REVIEW
    assert name.suggested_by == "Maa"
    assert "Zoya" in page(parent).content.decode()
    assert "Zoya" not in page(guest).content.decode()


def test_two_parents_voting_are_recognised(board):
    first, second = member(board, "a@example.com"), member(board, "b@example.com")
    name = Name.objects.create(board=board, text="Tara", key="tara", status=Name.Status.KEPT)
    post(first, "vote", name.pk)
    post(second, "vote", name.pk)
    assert "You both love this" in page(first).content.decode()


def test_vote_toggles(parent, board):
    name = Name.objects.create(board=board, text="Tara", key="tara", status=Name.Status.KEPT)
    post(parent, "vote", name.pk)
    post(parent, "vote", name.pk)
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


@pytest.mark.parametrize(
    ("route", "args"),
    [
        ("suggest", ()),
        ("rank", ()),
        ("brief", ()),
        ("keep", (1,)),
        ("pass", (1,)),
        ("split", (1,)),
        ("delete_board", ()),
        ("reset_family_link", ()),
    ],
)
def test_guest_link_cannot_use_parent_actions(guest, route, args):
    assert post(guest, route, *args).status_code == 404


def test_family_link_never_reveals_private_links(guest, board):
    html = page(guest).content.decode()
    assert board.parent_token not in html
    assert board.invite_token not in html


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
def test_a_missing_key_falls_back_to_the_catalogue_and_never_leaks_config(parent):
    from names.ai.base import get_ai

    get_ai.cache_clear()
    response = post(parent, "suggest")
    get_ai.cache_clear()
    assert response.status_code == 200
    assert "GEMINI" not in response.content.decode()


@override_settings(AI_BACKEND="names.ai.gemini.GeminiAI", GEMINI_API_KEY="secret-value-123")
def test_api_key_never_reaches_a_page(parent):
    assert "secret-value-123" not in page(parent).content.decode()


def test_ai_budget_blocks_a_flood_on_one_board(parent):
    statuses = {post(parent, "suggest").status_code for _ in range(31)}
    assert 429 in statuses


def test_security_headers_are_set(parent):
    response = page(parent)
    assert "script-src" in response["Content-Security-Policy"]
    assert response["Referrer-Policy"] == "strict-origin"


def test_healthchecks():
    assert Client().get("/healthz").content == b"ok"
    assert Client().get("/readyz").content == b"ok"


def test_family_submission_screen_layout_and_celebration(guest):
    html = page(guest).content.decode()
    assert "guest-hero" in html
    assert "guest-avatar-wrap" in html
    assert "guest-suggest-card" in html
    assert "guest-ideas-card" in html
    assert 'id="guest-sent-shelf"' in html

    added_resp = post(guest, "add", text="Ananya", by="Dadi")
    assert added_resp.status_code == 200
    added_html = added_resp.content.decode()
    assert "sent-celebration" in added_html
    assert "Sent with love!" in added_html
    assert "Thanks. Ananya is with the parents now." in added_html

