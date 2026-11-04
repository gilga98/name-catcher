import json

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from names import catalog, services
from names.accounts import profile_of, record_consent
from names.ai import base
from names.ai.base import NameIdea
from names.ai.stub import StubAI
from names.models import AICall, Board, CatalogName, Genre, Name

pytestmark = pytest.mark.django_db
HTMX = {"HTTP_HX_REQUEST": "true"}


class Spy(StubAI):
    def __init__(self):
        self.requests = []
        self.described = []

    def suggest(self, request):
        self.requests.append(request)
        return super().suggest(request)

    def describe_many(self, names, genres):
        self.described.append(names)
        return super().describe_many(names, genres)


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


@pytest.fixture
def spy(monkeypatch):
    fake = Spy()
    monkeypatch.setattr(services, "get_ai", lambda: fake)
    return fake


@pytest.fixture
def user():
    person = get_user_model().objects.create_user(username="p@example.com", email="p@example.com", password="x")
    record_consent(person)
    return person


@pytest.fixture
def board(user):
    board = Board.objects.create(brief={"surname": "Sharma", "avoid": "Rohan, Meena", "themes": ["Light"]})
    board.members.add(user)
    return board


@pytest.fixture
def client(user):
    client = Client()
    client.force_login(user)
    return client


def approved(text, **fields):
    defaults = {"key": catalog.phonetic_key(text), "text": text, "meaning": "A meaning", "status": "approved"}
    return CatalogName.objects.create(**{**defaults, **fields})


def test_only_useful_context_reaches_the_llm(board, spy):
    board.brief |= {"notes": "Born by the river. Call me on 98765 43210 or a@b.co", "nickname": "Chintu"}
    board.save()
    services.start_suggesting(board)
    sent = json.dumps(spy.requests[0].brief, ensure_ascii=False)
    assert "river" in sent and spy.requests[0].brief["themes"] == ["Light"]
    for private in ("Sharma", "Meena", "Rohan", "Chintu", "98765", "a@b.co"):
        assert private not in sent


def test_family_name_is_only_sent_after_opting_in(board, spy):
    board.brief["share_surname"] = True
    board.save()
    services.suggest_names(board)
    assert spy.requests[0].brief["surname"] == "Sharma"


def test_llm_output_is_kept_short_and_plain():
    idea = NameIdea(
        name="Tara", native_script="तारा", meaning="**Star** of " + "very " * 30, origin="Sanskrit",
        respelling="TAA-raa", sounds_like="taaraa", speech_lang="hi-IN",
        gender="girl", rarity="rare", style="any", genres=[],
        insight="# A " + "long " * 40, surname_fit="",
    )  # fmt: skip
    tidy = catalog.tidy_idea(idea)
    assert tidy.meaning.startswith("Star of") and len(tidy.meaning.split()) == 12
    assert "#" not in tidy.insight and len(tidy.insight.split()) == 20


def test_words_in_the_description_find_catalogue_genres():
    assert catalog.themes_from_text("Something brave, like the sunrise over a river") == ["Light", "Nature", "Strength"]


def test_cards_carry_tags_and_insights_from_the_catalogue(board):
    entry = approved("Ahana", origin="Sanskrit", rarity="rare", insight="Common in Bengal.")
    entry.genres.add(Genre.objects.get(name="Light"))
    name = services.add_from_catalog(board, [entry.pk])[0]
    assert name.tags == ["Sanskrit", "Rare", "Light"]
    assert name.insight == "Common in Bengal."


def test_llm_results_keep_a_local_surname_fit_and_drop_avoided_names(board, spy):
    names = services.suggest_names(board)
    assert names and all("Sharma" in name.surname_fit for name in names)
    assert "Rohan" not in [name.text for name in services.suggest_names(board)]


def test_the_prompt_is_told_which_names_are_overused(board, spy):
    approved("Tejasvini", picks=5)
    services.suggest_names(board)
    assert {"Aarav", "Tejasvini"} <= set(spy.requests[0].overused)


def test_paid_answers_wait_in_the_catalogue_for_review(board, spy):
    services.suggest_names(board)
    entry = CatalogName.objects.get(text="Aarav")
    assert (entry.status, entry.source) == ("pending", "llm")
    assert entry not in catalog.pick([entry.pk])


def test_catalogue_answers_details_without_an_llm_call(board, spy):
    approved("Ahana", meaning="Dawn")
    name = Name.objects.create(board=board, text="Ahaana", key=catalog.phonetic_key("Ahaana"))
    assert services.describe_name(name)
    name.refresh_from_db()
    assert name.meaning == "Dawn"
    assert spy.described == []
    assert not AICall.objects.exists()


def test_each_name_is_only_ever_described_once(board, spy):
    first = Name.objects.create(board=board, text="Zubin", key=catalog.phonetic_key("Zubin"))
    other_board = Board.objects.create(brief={})
    second = Name.objects.create(board=other_board, text="Zubin", key=catalog.phonetic_key("Zubin"))
    services.describe_name(first)
    services.describe_name(second)
    assert spy.described == [["Zubin"]]


def test_switching_ai_off_stops_all_llm_use(board, user, spy, client):
    profile = profile_of(user)
    profile.ai_enabled = False
    profile.save()
    with pytest.raises(base.AIError):
        services.start_suggesting(board)
    name = Name.objects.create(board=board, text="Zubin", key=catalog.phonetic_key("Zubin"))
    response = client.post(reverse("details", args=[board.parent_token, name.pk]), **HTMX)
    assert "isn't in our catalogue" in response.content.decode()
    assert b"hx-trigger" not in response.content  # No request loop.
    assert spy.requests == [] and spy.described == []


@override_settings(AI_FREE_CALLS_PER_MONTH=1)
def test_monthly_allowance_stops_llm_calls_with_a_clear_message(board, spy, client):
    assert client.post(reverse("suggest", args=[board.parent_token]), **HTMX).status_code == 200
    response = client.post(reverse("suggest", args=[board.parent_token]), **HTMX)
    assert response.status_code == 502
    assert b"used up" in response.content
    assert len(spy.requests) == 1


def test_cached_lookups_do_not_spend_the_ai_budget(board, client, spy):
    approved("Ahana")
    name = Name.objects.create(board=board, text="Ahana", key=catalog.phonetic_key("Ahana"))
    with override_settings(AI_RATE_PER_BOARD="0/h"):
        assert client.post(reverse("details", args=[board.parent_token, name.pk]), **HTMX).status_code == 200


def test_shard_is_filtered_by_brief_and_holds_no_meanings(board, client):
    light = Genre.objects.get(name="Light")
    ok = approved("Ahana", gender="girl", origin="Sanskrit")
    ok.genres.add(light)
    approved("Bhim", gender="boy", origin="Sanskrit")
    approved("Nila", gender="girl", origin="Tamil")
    CatalogName.objects.create(key="pending", text="Pending", gender="girl", origin="Sanskrit")
    board.brief |= {"gender": "girl", "origins": ["Sanskrit"]}
    board.save()
    body = client.get(reverse("catalog_shard", args=[board.parent_token])).json()
    assert [entry["t"] for entry in body["names"]] == ["Ahana"]
    assert body["names"][0]["g"] == ["Light"]
    assert "meaning" not in json.dumps(body) and "A meaning" not in json.dumps(body)


def test_guests_cannot_download_the_shard(board):
    assert Client().get(reverse("catalog_shard", args=[board.guest_token])).status_code == 404


def test_browser_picks_are_checked_again_on_the_server(board, client):
    good = approved("Ahana")
    hidden = CatalogName.objects.create(key="hidn", text="Hidden", status="rejected")
    avoided = approved("Rohan")
    Name.objects.create(board=board, text="Ahaana", key=catalog.phonetic_key("Ahaana"))
    fresh = approved("Tejas")
    url = reverse("suggest_from_catalog", args=[board.parent_token])
    client.post(url, {"id": [good.pk, hidden.pk, avoided.pk, fresh.pk]}, **HTMX)
    added = set(board.names.filter(source="catalog").values_list("text", flat=True))
    assert added == {"Tejas"}
    fresh.refresh_from_db()
    good.refresh_from_db()
    assert (fresh.picks, good.picks) == (1, 0)


def test_the_browsers_ranking_is_kept(board):
    picks = [approved("Zubin"), approved("Ahana"), approved("Mohan")]
    names = services.add_from_catalog(board, [entry.pk for entry in picks])
    assert [name.text for name in names] == ["Zubin", "Ahana", "Mohan"]


def test_new_board_page_lets_the_browser_go_first(board, client):
    html = client.get(reverse("board", args=[board.parent_token])).content.decode()
    assert 'id="engine-data"' in html
    assert 'data-is-new="true"' in html
    assert 'hx-trigger="load' not in html
    engine = html.split('id="engine-data" type="application/json">')[1].split("</script>")[0]
    assert "Sharma" not in engine


def test_user_contributions_stay_pending_and_cannot_edit_live_names(user, client):
    live = approved("Ahana", meaning="Dawn")
    light = Genre.objects.get(name="Light")
    response = client.post(
        reverse("contribute"),
        {
            "names": "Ahaana, something rude\nTejasvini, Radiant\n<script>, bad\n",
            "genres": ["Light"],
            "origin": "Sanskrit",
            "rights": "on",
        },
    )
    assert response.status_code == 200
    live.refresh_from_db()
    assert (live.meaning, live.submissions) == ("Dawn", 2)
    new = CatalogName.objects.get(text="Tejasvini")
    assert (new.status, new.source, new.meaning, new.origin) == ("pending", "user", "Radiant", "Sanskrit")
    assert list(new.genres.all()) == [light]
    assert new.submitted_by == user
    assert not CatalogName.objects.filter(text__contains="script").exists()
    assert user.consents.filter(purpose="contribution").exists()


def test_contributing_requires_the_rights_confirmation(client):
    client.post(reverse("contribute"), {"names": "Tejasvini"})
    assert not CatalogName.objects.exists()


def test_csv_import_reads_all_columns_and_reports_bad_rows():
    text = "name,meaning,origin,gender,genres,native,respelling,speech_lang,rarity,style\n"
    text += "Ahana,Dawn,Sanskrit,girl,Light|Nature,अहना,uh-HUH-naa,hi-IN,rare,classic\n,,,\n12345,x\n"
    result = catalog.import_rows(text, catalog.ImportOptions(source="admin", status="approved"))
    entry = CatalogName.objects.get()
    assert (result.created, len(result.errors)) == (1, 1)
    assert (entry.native, entry.rarity, entry.style) == ("अहना", "rare", "classic")
    assert sorted(genre.name for genre in entry.genres.all()) == ["Light", "Nature"]


def test_row_limit_is_enforced():
    options = catalog.ImportOptions(source="user", status="pending", row_limit=1)
    result = catalog.import_rows("Ahana\nTejas\n", options)
    assert result.created == 1 and "Stopped after 1" in result.errors[0]


@pytest.fixture
def staff():
    person = get_user_model().objects.create_superuser("a@example.com", "a@example.com", "x")
    client = Client()
    client.force_login(person)
    return client


def test_admin_bulk_upload_and_overview(staff):
    upload = SimpleUploadedFile("names.csv", b"name,meaning\nAhana,Dawn\n")
    url = reverse("admin:names_catalogname_upload")
    response = staff.post(url, {"file": upload, "publish": "on"}, follow=True)
    assert response.status_code == 200
    assert CatalogName.objects.get().status == "approved"
    assert b"Names live in the catalogue" in staff.get(reverse("admin:index")).content


def test_admin_cannot_see_inside_family_boards(staff, board):
    html = staff.get(reverse("admin:names_board_changelist")).content.decode()
    assert board.parent_token not in html and board.guest_token not in html and "Sharma" not in html
    assert staff.get("/manage/names/name/").status_code == 404


def test_admin_opens_with_just_username_and_password(client):
    get_user_model().objects.create_superuser("adminuser", "", "secret")
    login = client.get(reverse("admin:index"), follow=True).redirect_chain[-1][0]
    response = client.post(login, {"username": "adminuser", "password": "secret"}, follow=True)
    assert response.redirect_chain[-1][0] == reverse("admin:index")
    assert response.status_code == 200


def test_non_staff_cannot_reach_admin(client):
    response = client.get(reverse("admin:index"))
    assert response.status_code in (302, 403)
    assert client.get(reverse("admin:names_catalogname_upload")).status_code in (302, 403)


def test_admin_login_uses_username_not_email(client):
    get_user_model().objects.create_superuser("adminuser", "", "secret")
    response = client.get(reverse("admin:index"))
    assert response.status_code == 302
    assert response.headers["Location"] == f"{reverse('admin:login')}?next={reverse('admin:index')}"

    login_page = client.get(response.headers["Location"])
    assert login_page.status_code == 200
    assert b'name="username"' in login_page.content
    assert b'name="login"' not in login_page.content

    post_response = client.post(
        response.headers["Location"],
        {"username": "adminuser", "password": "secret"},
        follow=False,
    )
    assert post_response.status_code == 302
    assert post_response.headers["Location"] == reverse("admin:index")
    assert client.get(reverse("admin:index")).status_code == 200
