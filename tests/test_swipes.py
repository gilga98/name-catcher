from dataclasses import replace
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from names import catalog, services
from names.accounts import profile_of, record_consent
from names.ai.base import AIError, NameIdea
from names.ai.stub import StubAI
from names.jobs import LLM, drain
from names.models import Board, CatalogName, Job, Name, Swipe, Vote
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


def page(board, actor):
    return actor[0].get(reverse("board", args=[board.parent_token])).content.decode()


def test_a_keep_shows_on_the_partners_card_and_shortlist(board, parents, name):
    first, second = parents
    swipe(board, first, name, "keep")
    assert deck(board, second) == ["Tara"]
    card = services.deck_for(board, second[1])[0]
    assert (card.partner_note, card.partner_kept) == ("Your partner kept this", True)
    assert "Your partner kept this" in page(board, second)
    assert [n.text for n in services.shortlist(board, second[1])] == ["Tara"]


def test_a_pass_shows_on_the_partners_card(board, parents, name):
    first, second = parents
    swipe(board, first, name, "pass")
    card = services.deck_for(board, second[1])[0]
    assert (card.partner_note, card.partner_passed) == ("Your partner passed on this", True)
    assert "Your partner passed on this" in page(board, second)


def test_cards_use_the_partners_display_name_and_never_their_email(board, parents, name):
    first, second = parents
    profile = profile_of(get_user_model().objects.get(email="a@example.com"))
    profile.display_name = "Ganesh"
    profile.save()
    swipe(board, first, name, "keep")
    html = page(board, second)
    assert "Ganesh kept this" in html and "a@example.com" not in html


def test_the_keeper_sees_who_they_are_waiting_for(board, parents, name):
    first, _ = parents
    swipe(board, first, name, "keep")
    assert services.shortlist(board, first[1])[0].partner_note.startswith("Waiting for")


def test_family_never_see_a_parents_decisions(board, parents, name):
    first, _ = parents
    swipe(board, first, name, "keep")
    html = Client().get(reverse("board", args=[board.guest_token])).content.decode()
    assert "kept this" not in html and "Waiting for" not in html


def test_decisions_feed_gives_the_partner_live_updates(board, parents, name):
    first, second = parents
    url = reverse("decisions", args=[board.parent_token])
    before = second[0].get(url).json()
    assert before["notes"][str(name.pk)]["text"] == ""
    swipe(board, first, name, "keep")
    after = second[0].get(url).json()
    assert after["notes"][str(name.pk)] == {"text": "Your partner kept this", "kept": True}
    assert after["stamp"] != before["stamp"]


def test_a_relatives_suggestion_can_join_an_open_deck(board, parents):
    first, _ = parents
    Client().post(reverse("add", args=[board.guest_token]), {"text": "Zoya", "by": "Maa"}, **HTMX)
    name = board.names.get()
    url = reverse("new_cards", args=[board.parent_token])
    assert b"Zoya" in first[0].get(url, {"id": name.pk}).content


@pytest.mark.parametrize("route", ["decisions", "shortlist_panel", "new_cards"])
def test_family_links_cannot_read_parent_feeds(board, route):
    assert Client().get(reverse(route, args=[board.guest_token])).status_code == 404


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


def refresh(board):
    return Client().post(reverse("refresh_family_ideas", args=[board.guest_token]), **HTMX)


def ideas_on_show(response):
    stub_names = ("Aarav", "Anaya", "Ishaan", "Meera", "Nila", "Kavya", "Rohan", "Tara")
    return {name for name in stub_names if name.encode() in response.content}


def test_refreshing_family_ideas_asks_the_llm_for_different_names(board):
    parent(board, "a@example.com")
    first = ideas_on_show(Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX))
    second = ideas_on_show(refresh(board))
    assert first and second and not first & second
    assert board.members.get().ai_calls.count() == 2


def test_refreshed_names_are_what_every_relative_sees_next(board):
    parent(board, "a@example.com")
    Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX)
    second = ideas_on_show(refresh(board))
    assert ideas_on_show(Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX)) == second
    assert board.members.get().ai_calls.count() == 2


def test_refreshed_names_come_before_the_catalogue(board):
    parent(board, "a@example.com")
    for text in ("Ahana", "Tejas", "Oviya", "Nilima", "Rudra", "Vanya", "Charu", "Dhruv", "Gauri"):
        CatalogName.objects.create(key=catalog.phonetic_key(text), text=text, meaning="m", status="approved")
    first = Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX)
    assert not ideas_on_show(first)
    html = refresh(board).content.decode()
    assert html.index("Aarav") < html.index("Gauri")
    assert "Ahana" not in html  # Shown before the refresh, so it has moved on.


def test_a_failed_refresh_keeps_the_list_and_says_so(board, monkeypatch):
    parent(board, "a@example.com")
    first = ideas_on_show(Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX))

    class Broken:
        def suggest(self, request):
            raise AIError("Try again in a minute.")

    monkeypatch.setattr(services, "get_ai", Broken)
    response = refresh(board)
    assert ideas_on_show(response) == first and b"No new ideas" in response.content
    # Only the person who asked gets told. Someone opening the link later just sees ideas.
    assert b"No new ideas" not in Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX).content


def test_a_refresh_that_finds_only_repeats_keeps_the_list(board, monkeypatch):
    parent(board, "a@example.com")
    first = ideas_on_show(Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX))
    stub = StubAI()

    class Repeating:
        def suggest(self, request):
            return stub.suggest(replace(request, shown=[]))

    monkeypatch.setattr(services, "get_ai", Repeating)
    response = refresh(board)
    assert ideas_on_show(response) == first and b"No new ideas" in response.content


def test_relatives_cant_use_the_allowance_the_parents_need(board):
    parent(board, "a@example.com")
    with override_settings(AI_FREE_CALLS_PER_MONTH=22):
        assert b"New ideas" in Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX).content
        board.members.get().ai_calls.create(kind="suggest")
        response = refresh(board)
        assert b"New ideas" not in response.content and b"No new ideas" in response.content
    assert board.members.get().ai_calls.count() == 2


@override_settings(FAMILY_REFRESH_RATE="2/d")
def test_refreshes_are_capped_per_board(board):
    parent(board, "a@example.com")
    Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX)
    calls = board.members.get().ai_calls
    before = calls.count()
    refresh(board), refresh(board)
    assert calls.count() == before + 2
    assert b"No new ideas" in refresh(board).content
    assert calls.count() == before + 2


def test_no_refresh_when_a_parent_has_switched_ai_off(board):
    parent(board, "a@example.com")
    user = board.members.get()
    profile = profile_of(user)
    profile.ai_enabled = False
    profile.save()
    assert b"New ideas" not in Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX).content
    refresh(board)
    assert user.ai_calls.count() == 0


@override_settings(JOBS_EAGER=False)
def test_tapping_refresh_twice_makes_one_call_and_shows_it_is_working(board):
    parent(board, "a@example.com")
    assert b"Finding more ideas" in refresh(board).content
    refresh(board)
    assert Job.objects.filter(board=board, kind="family_ideas").count() == 1


def test_names_to_avoid_never_reach_relatives(board):
    parent(board, "a@example.com")
    CatalogName.objects.create(key=catalog.phonetic_key("Meena"), text="Meena", meaning="m", status="approved")
    assert b"Meena" not in Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX).content


def test_a_refresh_prompt_holds_only_names_relatives_were_shown(board, monkeypatch):
    parent(board, "a@example.com")
    Name.objects.create(board=board, text="Keptname", key="keptname", status=Name.Status.KEPT)
    Client().post(reverse("add", args=[board.guest_token]), {"text": "Typedbyanaunt", "by": "Aunt"})
    requests = []
    stub = StubAI()

    class Recording:
        def suggest(self, request):
            requests.append(request)
            return stub.suggest(request)

    monkeypatch.setattr(services, "get_ai", Recording)
    Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX)
    refresh(board)
    assert set(requests[1].shown) == {"Aarav", "Anaya", "Ishaan", "Meera"}
    assert "Keptname" not in str(requests[1]) and "Typedbyanaunt" not in str(requests[1])


def preview(board, text="Oviya", **extra):
    return Client().post(reverse("preview_name", args=[board.guest_token]), {"text": text, **extra}, **HTMX)


def known_name(text="Oviya"):
    key = catalog.phonetic_key(text)
    return CatalogName.objects.create(
        key=key, text=text, meaning="Painting", respelling="OH-vee-yaa", origin="Tamil", status="approved"
    )


def test_a_preview_shows_the_enriched_card_and_shares_nothing(board):
    parent(board, "a@example.com")
    known_name()
    guest = Client()
    html = guest.post(reverse("preview_name", args=[board.guest_token]), {"text": "Oviya"}, **HTMX).content.decode()
    assert "Painting" in html and "OH-vee-yaa" in html and "Tamil" in html and "Rao" in html
    assert not board.names.exists() and not Vote.objects.exists() and "voter" not in guest.session


def test_the_previewed_card_is_what_the_parents_get(board):
    client, _ = parent(board, "a@example.com")
    known_name()
    preview(board)
    Client().post(reverse("add", args=[board.guest_token]), {"text": "Oviya", "by": "Maa"}, **HTMX)
    name = board.names.get()
    assert (name.meaning, name.respelling, name.suggested_by) == ("Painting", "OH-vee-yaa", "Maa")
    assert "Painting" in client.get(reverse("board", args=[board.parent_token])).content.decode()


def test_a_preview_cant_reveal_what_is_on_the_board(board):
    parent(board, "a@example.com")
    known_name()
    before = preview(board).content
    Name.objects.create(board=board, text="Oviya", key=catalog.phonetic_key("Oviya"), status=Name.Status.KEPT)
    assert preview(board).content == before


def test_a_preview_shows_nothing_only_parents_should_see(board):
    parent(board, "a@example.com")
    known_name()
    board.birth_star = {"letter": "O", "letters": ["O"]}
    board.save()
    html = preview(board).content.decode()
    for private in ("Chintu", "Aunt Meena", "Birth star", "Keep", "Similar"):
        assert private not in html


def test_an_unknown_name_is_described_while_the_card_is_open(board):
    parent(board, "a@example.com")
    with override_settings(JOBS_EAGER=False):
        html = preview(board, "Zorvika").content.decode()
        assert "Adding tags and meaning" in html and 'name="text" value="Zorvika"' in html
        assert 'hx-include="this"' in html and "closest dialog" not in html
        assert not board.names.exists()
        still_pending = preview(board, "Zorvika", checking="1").content.decode()
        assert "Adding tags and meaning" in still_pending and 'hx-include="this"' in still_pending
        assert 'name="text" value="Zorvika"' in still_pending
    drain(LLM)
    response = preview(board, "Zorvika", checking="1").content.decode()
    assert "A name your family suggested" in response and "every 2s" not in response


def test_a_preview_pays_for_nothing_when_the_name_is_already_on_the_board(board):
    parent(board, "a@example.com")
    Name.objects.create(board=board, text="Muhammad", key=catalog.phonetic_key("Muhammad"), status=Name.Status.KEPT)
    with override_settings(JOBS_EAGER=False):
        first = preview(board, "Mohammed").content.decode()
    assert "Adding tags and meaning" in first and "Muhammad" not in first
    assert not CatalogName.objects.exists()
    checked = preview(board, "Mohammed", checking="1").content.decode()
    assert "catalogue yet" in checked and "Muhammad" not in checked


def ask_for_family_idea(board, monkeypatch, text="Oviya"):
    answer = NameIdea(
        name=text,
        native_script="ஓவியா",
        meaning="Painting",
        origin="Tamil",
        respelling="OH-vee-yaa",
        sounds_like="oviya",
        speech_lang="ta-IN",
        gender="girl",
        rarity="rare",
        style="any",
        genres=[],
        insight="Soft sounds",
        surname_fit="",
    )
    monkeypatch.setattr("names.services.get_ai", lambda: SimpleNamespace(suggest=lambda request: [answer]))
    services.fetch_family_ideas(board)


def test_a_family_idea_is_not_described_a_second_time(board, monkeypatch):
    parent(board, "a@example.com")
    ask_for_family_idea(board, monkeypatch)
    monkeypatch.setattr(catalog, "request_enrichment", lambda *args: pytest.fail("the LLM already described this"))
    assert "Painting" in preview(board).content.decode()
    Client().post(reverse("add", args=[board.guest_token]), {"text": "Oviya", "by": "Maa"}, **HTMX)
    name = board.names.get()
    assert (name.meaning, name.respelling, name.insight) == ("Painting", "OH-vee-yaa", "Soft sounds")
    assert name.suggested_by == "Maa"


def test_another_spelling_of_a_family_idea_reuses_it_too(board, monkeypatch):
    parent(board, "a@example.com")
    ask_for_family_idea(board, monkeypatch, "Aarav")
    monkeypatch.setattr(catalog, "request_enrichment", lambda *args: pytest.fail("the LLM already described this"))
    assert "Painting" in preview(board, "Arav").content.decode()


def test_a_family_answer_stays_on_the_board_that_asked(board, monkeypatch):
    parent(board, "a@example.com")
    ask_for_family_idea(board, monkeypatch)
    other = Board.objects.create(brief={})
    parent(other, "b@example.com")
    with override_settings(JOBS_EAGER=False):
        assert "Painting" not in preview(other).content.decode()


def test_ideas_stored_before_answers_were_kept_still_work(board):
    parent(board, "a@example.com")
    idea = {"text": "Oviya", "meaning": "Painting"}
    board.family_ideas = {"stamp": catalog.brief_stamp(board.brief), "names": [idea]}
    board.save()
    with override_settings(JOBS_EAGER=False):
        assert "Adding tags and meaning" in preview(board).content.decode()


def test_checking_on_a_card_cant_start_a_description(board):
    parent(board, "a@example.com")
    assert b"catalogue yet" in preview(board, "Zorvika", checking="1").content
    assert not CatalogName.objects.exists()


def test_a_name_stays_off_the_llm_when_a_parent_has_switched_ai_off(board):
    parent(board, "a@example.com")
    profile = profile_of(board.members.get())
    profile.ai_enabled = False
    profile.save()
    assert b"catalogue yet" in preview(board, "Zorvika").content
    assert not CatalogName.objects.exists()
    assert b"AI may describe" not in Client().get(reverse("board", args=[board.guest_token])).content


def test_a_preview_rejects_what_isnt_a_name(board):
    assert preview(board, "<script>alert(1)</script>").status_code == 422
    assert not CatalogName.objects.exists()


@override_settings(ADD_RATE_PER_BOARD="2/h")
def test_previews_are_limited_but_checking_on_one_is_not(board):
    parent(board, "a@example.com")
    known_name()
    assert [preview(board).status_code for _ in range(3)] == [200, 200, 429]
    assert preview(board, checking="1").status_code == 200


def test_a_relative_can_change_or_clear_their_name_before_sending(board):
    parent(board, "a@example.com")
    known_name()
    guest = Client()
    url = reverse("add", args=[board.guest_token])
    preview_url = reverse("preview_name", args=[board.guest_token])
    guest.post(url, {"text": "Tejas", "by": "Maa"}, **HTMX)
    assert b'value="Maa"' in guest.post(preview_url, {"text": "Oviya"}, **HTMX).content
    guest.post(url, {"text": "Oviya", "by": ""}, **HTMX)
    assert board.names.get(text="Oviya").suggested_by == "Family"
    assert b'value="Maa"' not in guest.post(preview_url, {"text": "Nila"}, **HTMX).content


def test_a_bad_name_for_the_sender_says_so(board):
    response = Client().post(reverse("add", args=[board.guest_token]), {"text": "Oviya", "by": "Maa2"}, **HTMX)
    assert response.status_code == 422 and b"your name" in response.content


def test_tapping_an_idea_opens_a_preview_rather_than_sending_it(board):
    parent(board, "a@example.com")
    known_name()
    html = Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX).content.decode()
    assert reverse("preview_name", args=[board.guest_token]) in html
    assert reverse("add", args=[board.guest_token]) + '"' not in html


def test_tapped_ideas_leave_the_list(board):
    parent(board, "a@example.com")
    url = reverse("family_ideas", args=[board.guest_token])
    Client().post(url, **HTMX)
    Client().post(reverse("add", args=[board.guest_token]), {"text": "Aarav"}, **HTMX)
    assert b"Aarav" not in Client().post(url, **HTMX).content


def test_preview_name_with_no_ai_skips_llm_enrichment(board):
    parent(board, "a@example.com")
    with override_settings(JOBS_EAGER=False):
        response = Client().post(
            reverse("preview_name", args=[board.guest_token]),
            {"text": "Zorvika", "no_ai": "1"},
            **HTMX,
        )
        assert response.status_code == 200
        assert "catalogue yet" in response.content.decode()
        assert not CatalogName.objects.exists()

