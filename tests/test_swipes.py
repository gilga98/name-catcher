import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from names import catalog, services
from names.accounts import record_consent
from names.models import Board, CatalogName, Name, Swipe
from names.services import Voter

pytestmark = pytest.mark.django_db
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


def parent(board, email):
    user = get_user_model().objects.create_user(username=email, email=email, password="x")
    record_consent(user)
    board.members.add(user)
    client = Client()
    client.force_login(user)
    return client, Voter(id=f"user-{user.pk}", name="Parent", is_parent=True)


@pytest.fixture
def board():
    return Board.objects.create(brief={"surname": "Rao", "nickname": "Chintu", "avoid": "Aunt Meena"})


@pytest.fixture
def parents(board):
    return parent(board, "a@example.com"), parent(board, "b@example.com")


@pytest.fixture
def name(board):
    return Name.objects.create(board=board, text="Tara", key="tara")


def swipe(board, actor, name, route):
    client, _ = actor
    response = client.post(reverse(route, args=[board.parent_token, name.pk]), **HTMX)
    assert response.status_code == 200
    name.refresh_from_db()


def deck(board, actor):
    return [n.text for n in services.deck_for(board, actor[1])]


def test_rejected_by_one_stays_alive_for_the_other(board, parents, name):
    first, second = parents
    swipe(board, first, name, "pass")
    assert name.status == Name.Status.REVIEW
    assert deck(board, first) == []
    assert deck(board, second) == ["Tara"]


def test_rejected_by_both_is_binned(board, parents, name):
    first, second = parents
    swipe(board, first, name, "pass")
    swipe(board, second, name, "pass")
    assert name.status == Name.Status.PASSED
    assert services.binned(board) == [name]
    assert deck(board, second) == []


def test_loved_by_both_is_a_match(board, parents, name):
    first, second = parents
    swipe(board, first, name, "keep")
    swipe(board, second, name, "keep")
    html = first[0].get(reverse("board", args=[board.parent_token])).content.decode()
    assert "You both love this" in html


def test_decisions_are_blind_until_you_have_decided(board, parents, name):
    first, second = parents
    swipe(board, first, name, "keep")
    assert [n.text for n in services.shortlist(board, first[1])] == ["Tara"]
    assert services.shortlist(board, second[1]) == []
    assert deck(board, second) == ["Tara"]


def test_a_split_decision_is_flagged_for_both_and_hidden_from_family(board, parents, name):
    first, second = parents
    swipe(board, first, name, "keep")
    swipe(board, second, name, "pass")
    assert name.status == Name.Status.KEPT
    assert services.shortlist(board, second[1])[0].split
    assert "Talk about this one" in first[0].get(reverse("board", args=[board.parent_token])).content.decode()
    guest = Voter(id="guest", name="Family", is_parent=False)
    assert services.shortlist(board, guest) == []


def test_undo_returns_the_card_to_the_deck(board, parents, name):
    first, _ = parents
    swipe(board, first, name, "pass")
    swipe(board, first, name, "undo")
    assert deck(board, first) == ["Tara"]
    assert not Swipe.objects.exists()


def test_undo_keeps_the_partners_decision(board, parents, name):
    first, second = parents
    swipe(board, first, name, "keep")
    swipe(board, second, name, "pass")
    swipe(board, first, name, "undo")
    assert name.status == Name.Status.REVIEW
    assert Swipe.objects.get().voter == second[1].id


def test_binned_names_can_come_back_for_both(board, parents, name):
    first, second = parents
    swipe(board, first, name, "pass")
    swipe(board, second, name, "pass")
    first[0].post(reverse("restore", args=[board.parent_token, name.pk]), **HTMX)
    name.refresh_from_db()
    assert name.status == Name.Status.REVIEW
    assert deck(board, first) == deck(board, second) == ["Tara"]


def test_solo_parent_bins_alone_but_a_new_partner_gets_a_say(board, name):
    first = parent(board, "a@example.com")
    swipe(board, first, name, "pass")
    assert name.status == Name.Status.PASSED
    partner = get_user_model().objects.create_user(username="c@example.com", email="c@example.com", password="x")
    services.join_board(board, partner)
    name.refresh_from_db()
    assert name.status == Name.Status.REVIEW
    assert [n.text for n in services.deck_for(board, Voter(f"user-{partner.pk}", "P", True))] == ["Tara"]


def test_family_suggestions_join_both_decks(board, parents):
    first, second = parents
    Client().post(reverse("add", args=[board.guest_token]), {"text": "Zoya", "by": "Maa"}, **HTMX)
    assert deck(board, first) == deck(board, second) == ["Zoya"]


def test_guest_page_shows_the_parents_context_but_never_names_to_avoid(board):
    board.brief |= {"notes": "We met by the sea", "share_notes": True, "origins": ["Tamil"], "gender": "girl"}
    board.save()
    html = Client().get(reverse("board", args=[board.guest_token])).content.decode()
    assert "We met by the sea" in html and "Tamil" in html and "Chintu" in html
    assert "Aunt Meena" not in html


def test_parents_can_keep_their_description_from_family(board):
    board.brief |= {"notes": "Private thoughts", "share_notes": False}
    board.save()
    assert "Private thoughts" not in Client().get(reverse("board", args=[board.guest_token])).content.decode()


def test_family_ideas_use_the_catalogue_without_an_llm_call(board, monkeypatch):
    monkeypatch.setattr(services, "get_ai", lambda: pytest.fail("no LLM needed"))
    for text in ("Ahana", "Tejas", "Oviya", "Nila"):
        CatalogName.objects.create(key=catalog.phonetic_key(text), text=text, meaning="m", status="approved")
    response = Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX)
    assert b"Ahana" in response.content


def test_family_ideas_call_the_llm_once_per_brief_however_many_relatives_visit(board):
    url = reverse("family_ideas", args=[board.guest_token])
    parent(board, "a@example.com")
    Client().post(url, **HTMX)
    assert b"Aarav" in Client().post(url, **HTMX).content
    assert board.members.get().ai_calls.count() == 1
    board.refresh_from_db()
    board.brief["notes"] = "Changed our minds"
    board.save()
    Client().post(url, **HTMX)
    assert board.members.get().ai_calls.count() == 2


def test_tapped_ideas_leave_the_list(board):
    parent(board, "a@example.com")
    url = reverse("family_ideas", args=[board.guest_token])
    Client().post(url, **HTMX)
    Client().post(reverse("add", args=[board.guest_token]), {"text": "Aarav"}, **HTMX)
    assert b"Aarav" not in Client().post(url, **HTMX).content
