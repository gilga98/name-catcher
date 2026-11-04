import json
from datetime import timedelta

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from names import jobs, places, services, soota
from names.accounts import export_data, record_consent
from names.ai.base import NameIdea
from names.ai.context import brief_for_llm
from names.models import Board, CatalogName, ConsentRecord, Job, Name

pytestmark = pytest.mark.django_db
HTMX = {"HTTP_HX_REQUEST": "true"}
SOOTA_ON = {"SOOTA_API_URL": "https://soota.test", "SOOTA_API_KEY": "secret-key"}
PARK = {"JOBS_EAGER": False}  # Work waits in the queue, as it does between a request and the worker.
DELHI = {"label": "Delhi, India", "latitude": 28.65195, "longitude": 77.23149, "timezone": "Asia/Kolkata"}
FORM = {"birth_date": "1990-01-15", "birth_time": "10:30", "place": places.sign(DELHI), "consent": "on"}
STAR = {
    "nakshatra": "Purva Phalguni", "pada": 3, "sign": "Leo", "letter": "Ti", "letters": ["Mo", "Ta", "Ti", "Tu"],
    "birth_number": 6, "destiny_number": 8,
}  # fmt: skip
# Soota's own example answer for these birth details, cut down to what shows the shape. It holds far more we ignore.
NATAL = {
    "chart": {"lagna": {"sign": 11, "sign_name": "Pisces"}, "planets": [], "houses": [], "aspects": []},
    "panchanga": {"nakshatra": "Purva Phalguni", "pada": 3, "nama_letter": "Ti", "nama_letters": "Mo, Ta, Ti, Tu"},
    "functional_nature": {"lagna_sign": "Pisces", "planet_roles": [], "benefics": [], "malefics": [], "marakas": []},
    "avakahada": {
        "varna": "Kshatriya", "vashya": "Vanachara", "yoni": "Rat", "gana": "Manushya", "nadi": "Madhya",
        "nakshatra": "Purva Phalguni", "pada": 3, "nama_letters": "Mo, Ta, Ti, Tu", "nama_letter": "Ti", "sign": "Leo",
        "paksha": "Krishna", "maasa": "Pausha", "samvatsara": "Shukla",
    },
    "lucky_details": {"numbers": "2, 3, 9", "colors": "Yellow", "days": "Monday"},
}  # fmt: skip


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


def numerology_answer(request):
    birth = {"birth_number": 6, "birth_planet": "Venus", "destiny_number": 8, "destiny_planet": "Saturn"}
    rows = [{"name": name, "total": 11, "number": 2, "planet": "Moon"} for name in request["names"]]
    return {"system": request["system"], "names": rows, "birth": birth if request.get("birth_date") else None}


class FakeSoota:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.body, self.error = status, NATAL if body is None else body, error
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error:
            raise self.error
        body = self.body
        if url.endswith("/v1/numerology") and self.status == 200 and body is NATAL:
            body = numerology_answer(kwargs["json"])
        return httpx.Response(self.status, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def api(monkeypatch):
    fake = FakeSoota()
    monkeypatch.setattr(soota.httpx, "post", fake)
    return fake


@pytest.fixture
def parent():
    user = get_user_model().objects.create_user(username="p@example.com", email="p@example.com", password="x")
    record_consent(user)
    return user


@pytest.fixture
def board(parent):
    return services.create_board(parent, {"surname": "Rao"})


@pytest.fixture
def client(parent):
    client = Client()
    client.force_login(parent)
    return client


def read_url(board, name="read_birth_star"):
    return reverse(name, args=[board.parent_token])


def everything_stored(board) -> str:
    """Whatever the database still holds for this board, as text that can be searched for birth details."""
    board.refresh_from_db()
    rows = [board.brief, board.birth_star, board.family_ideas, *Job.objects.values("payload", "result")]
    return json.dumps(rows, default=str)


@override_settings(**SOOTA_ON)
def test_a_reading_stores_the_star_and_nothing_about_the_birth(client, board, api):
    response = client.post(read_url(board), FORM, **HTMX)
    assert response.headers["HX-Refresh"] == "true"
    board.refresh_from_db()
    assert board.birth_star == STAR
    assert not Job.objects.filter(board=board).exists()
    stored = everything_stored(board)
    for detail in ("1990", "10:30", "28.65", "Delhi"):
        assert detail not in stored


@override_settings(**SOOTA_ON)
def test_the_request_is_exactly_what_soota_accepts(client, board, api):
    client.post(read_url(board), FORM, **HTMX)
    assert api.calls[1]["url"] == "https://soota.test/v1/numerology"
    assert api.calls[1]["json"] == {"names": [], "system": "chaldean", "birth_date": "1990-01-15"}
    call = api.calls[0]
    assert call["url"] == "https://soota.test/v1/natal"
    assert call["headers"] == {"Authorization": "Bearer secret-key"}
    assert call["trust_env"] is False
    assert call["json"] == {
        "birth": {
            "dob": "1990-01-15",
            "tob": "10:30:00",
            "latitude": 28.65195,
            "longitude": 77.23149,
            "timezone_offset": 5.5,
            "ayanamsha": 2,
            "use_true_node": False,
        }
    }


def test_the_panel_stays_away_until_soota_is_configured(client, board, api):
    assert b"birth-star" not in client.get(reverse("board", args=[board.parent_token])).content
    assert client.post(read_url(board), FORM, **HTMX).status_code == 404
    assert client.get(read_url(board, "birth_star"), **HTMX).status_code == 404
    assert not api.calls


@override_settings(**SOOTA_ON)
def test_the_panel_shows_on_the_board(client, board):
    content = client.get(reverse("board", args=[board.parent_token])).content
    assert b"Add the birth star" in content


@override_settings(**SOOTA_ON)
def test_relatives_can_neither_use_nor_see_it(board, api):
    board.birth_star = STAR
    board.save()
    guest = Client()
    page = guest.get(reverse("board", args=[board.guest_token])).content.decode()
    for private in ("birth-star", "Purva Phalguni", "birth star sound"):
        assert private not in page
    assert guest.get(reverse("birth_star", args=[board.guest_token]), **HTMX).status_code == 404
    for route in ("read_birth_star", "remove_birth_star"):
        assert guest.post(reverse(route, args=[board.guest_token]), **HTMX).status_code == 404
    board.refresh_from_db()
    assert board.birth_star == STAR


@override_settings(**SOOTA_ON)
def test_someone_outside_the_board_cant_use_it(board, api):
    other = get_user_model().objects.create_user(username="o@example.com", email="o@example.com", password="x")
    record_consent(other)
    client = Client()
    client.force_login(other)
    assert client.post(read_url(board), FORM, **HTMX).status_code == 404
    assert not api.calls


@override_settings(**SOOTA_ON)
def test_nothing_is_sent_without_consent(client, board, api):
    response = client.post(read_url(board), {**FORM, "consent": ""}, **HTMX)
    assert b"Tick the box" in response.content
    assert not api.calls
    assert not ConsentRecord.objects.filter(purpose=ConsentRecord.Purpose.BIRTH_STAR).exists()


@override_settings(**SOOTA_ON)
def test_consent_is_recorded_for_the_parent(client, board, parent, api):
    client.post(read_url(board), FORM, **HTMX)
    record = ConsentRecord.objects.get(purpose=ConsentRecord.Purpose.BIRTH_STAR)
    assert (record.user, record.action) == (parent, ConsentRecord.Action.GIVEN)


@override_settings(**SOOTA_ON)
def test_a_birth_still_to_come_is_refused(client, board, api):
    tomorrow = (timezone.now() + timedelta(days=1)).date().isoformat()
    response = client.post(read_url(board), {**FORM, "birth_date": tomorrow}, **HTMX)
    assert b"after the birth" in response.content
    assert not api.calls


@pytest.mark.parametrize("field,value", [("birth_date", "soon"), ("birth_time", "noon"), ("place", "atlantis")])
@override_settings(**SOOTA_ON)
def test_bad_details_are_refused_before_anything_is_sent(client, board, api, field, value):
    assert client.post(read_url(board), {**FORM, field: value}, **HTMX).status_code == 200
    assert not api.calls


@pytest.mark.parametrize(
    "fake",
    [
        FakeSoota(status=503, body={"code": "engine_busy"}),
        FakeSoota(status=422, body={"code": "invalid_input"}),
        FakeSoota(status=302, body={}),
        FakeSoota(error=httpx.ConnectTimeout("too slow")),
        FakeSoota(body={"avakahada": {"nakshatra": "Purva Phalguni"}}),
        FakeSoota(body={"avakahada": {**NATAL["avakahada"], "pada": 9}}),
    ],
)
@override_settings(**SOOTA_ON)
def test_any_failure_stores_nothing_and_clears_the_details(client, board, monkeypatch, fake):
    monkeypatch.setattr(soota.httpx, "post", fake)
    client.post(read_url(board), FORM, **HTMX)
    job = Job.objects.get(board=board)
    if job.status == Job.Status.QUEUED:  # The one retry, once its wait is over.
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        jobs.drain(jobs.SOOTA)
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED
    assert job.payload == {}
    assert "1990" not in everything_stored(board)
    assert board.birth_star == {}
    response = client.get(read_url(board, "birth_star"), **HTMX)
    assert b"Try again in a minute" in response.content


@override_settings(**SOOTA_ON)
def test_trying_again_after_a_failure_works(client, board, monkeypatch):
    monkeypatch.setattr(soota.httpx, "post", FakeSoota(status=500, body={}))
    client.post(read_url(board), FORM, **HTMX)
    Job.objects.update(run_after=timezone.now())
    jobs.drain(jobs.SOOTA)
    monkeypatch.setattr(soota.httpx, "post", FakeSoota())
    assert client.post(read_url(board), FORM, **HTMX).headers["HX-Refresh"] == "true"
    board.refresh_from_db()
    assert board.birth_star == STAR
    assert not Job.objects.filter(board=board).exists()


@override_settings(**SOOTA_ON, **PARK)
def test_the_panel_waits_then_reloads_the_page(client, board, api):
    assert b"Working out" in client.post(read_url(board), FORM, **HTMX).content
    assert not api.calls  # The person isn't kept waiting on Soota.
    assert b"every 2s" in client.get(read_url(board, "birth_star"), **HTMX).content
    jobs.drain(jobs.SOOTA)
    assert client.get(read_url(board, "birth_star"), **HTMX).headers["HX-Refresh"] == "true"


@override_settings(**SOOTA_ON, **PARK)
def test_asking_twice_only_asks_soota_once(client, board, api):
    client.post(read_url(board), FORM, **HTMX)
    client.post(read_url(board), FORM, **HTMX)
    assert Job.objects.filter(board=board).count() == 1


@override_settings(**SOOTA_ON, **PARK)
def test_details_stuck_in_the_queue_dont_wait_out_a_long_outage(client, board, api):
    client.post(read_url(board), FORM, **HTMX)
    Job.objects.update(created_at=timezone.now() - timedelta(hours=2))
    call_command("run_jobs", verbosity=0)
    assert not Job.objects.filter(board=board).exists()
    assert not api.calls


@pytest.mark.parametrize(
    "url", ["http://soota.example.com", "http://8.8.8.8:8000", "http://[2001:4860:4860::8888]:8000", "http://"]
)
def test_birth_details_never_cross_the_internet_unencrypted(client, board, api, url):
    with override_settings(SOOTA_API_URL=url):
        client.post(read_url(board), FORM, **HTMX)
    assert not api.calls


@pytest.mark.parametrize("url", ["http://10.0.1.5:8000", "http://172.18.0.4:8000", "http://[::1]:8000"])
def test_private_addresses_are_allowed(api, url):
    with override_settings(SOOTA_API_URL=url):
        assert soota.read_birth_star({"dob": "1990-01-15"})["letter"] == "Ti"


@override_settings(SOOTA_API_URL="http://soota-engine-gateway:8000/")
def test_the_coolify_network_name_is_allowed(api):
    assert soota.read_birth_star({"dob": "1990-01-15"})["letter"] == "Ti"
    assert api.calls[0]["url"] == "http://soota-engine-gateway:8000/v1/natal"


@override_settings(**SOOTA_ON)
def test_a_refused_key_is_logged_without_the_details(client, board, monkeypatch, caplog):
    monkeypatch.setattr(soota.httpx, "post", FakeSoota(status=401, body={"code": "unauthorized"}))
    client.post(read_url(board), FORM, **HTMX)
    assert "Soota answered 401" in caplog.text
    assert "1990" not in caplog.text


@pytest.mark.parametrize("url", ["http://127.0.0.1:8001", "http://localhost:8001/"])
def test_loopback_is_fine_for_local_work(api, url):
    with override_settings(SOOTA_API_URL=url):
        assert soota.read_birth_star({"dob": "1990-01-15"})["letter"] == "Ti"
    assert api.calls[0]["url"].endswith(":8001/v1/natal")


@override_settings(**SOOTA_ON)
def test_saving_the_brief_keeps_the_star(client, board):
    board.birth_star = STAR
    board.save()
    client.post(reverse("brief", args=[board.parent_token]), {"surname": "Rao"})
    board.refresh_from_db()
    assert board.birth_star == STAR


@override_settings(**SOOTA_ON)
def test_removing_it_deletes_the_star_and_records_withdrawal(client, board, parent):
    board.birth_star = STAR
    board.save()
    client.post(read_url(board, "remove_birth_star"), **HTMX)
    board.refresh_from_db()
    assert board.birth_star == {}
    record = ConsentRecord.objects.get(purpose=ConsentRecord.Purpose.BIRTH_STAR)
    assert (record.user, record.action) == (parent, ConsentRecord.Action.WITHDRAWN)


def test_it_comes_with_a_data_export(board, parent):
    board.birth_star = STAR
    board.save()
    assert export_data(parent)["boards"][0]["birth_star"] == STAR


def test_a_board_without_a_star_sends_no_sounds():
    assert "birth_sounds" not in brief_for_llm({"style": "classic"}, {})


def test_the_llm_gets_the_sounds_and_nothing_else_about_the_star(board):
    board.birth_star = STAR
    request = services._suggestion_request(board)
    assert request.brief["birth_sounds"] == ["Mo", "Ta", "Ti", "Tu"]
    sent = json.dumps(request.brief)
    for private in ("Purva", "Leo", "pada", "nakshatra"):
        assert private not in sent


def test_relatives_ideas_dont_use_the_star(board):
    board.birth_star = STAR
    assert "birth_sounds" not in services._family_request(board).brief


def test_the_browser_gets_keys_it_can_compare_with_names(board, parent):
    board.birth_star = STAR
    voter = services.Voter(id="user-1", name="P", is_parent=True)
    state = services.engine_context(board, voter)
    assert state["birthSound"] == "ti"
    assert state["birthSounds"] == ["mo", "ta", "ti", "tu"]
    assert services.engine_context(Board.objects.create(), voter)["birthSounds"] == []


def test_sounds_match_names_across_spellings_and_scripts(board):
    board.birth_star = {**STAR, "letter": "Che", "letters": ["Chu", "Che", "Cho", "La"]}
    sounds = services.birth_sounds(board)
    assert services.phonetic_key("Chetan").startswith(tuple(sounds))
    assert services.phonetic_key("चेतन").startswith(tuple(sounds))
    assert services.phonetic_key("Laxmi").startswith(tuple(sounds))
    assert not services.phonetic_key("Tejas").startswith(tuple(sounds))


@override_settings(**SOOTA_ON)
def test_cards_say_when_a_name_has_a_birth_sound(client, board):
    board.birth_star = STAR
    board.save()
    Name.objects.create(board=board, text="Tilak", key="tilak")
    Name.objects.create(board=board, text="Gauri", key="gauri")
    content = client.get(reverse("board", args=[board.parent_token])).content.decode()
    assert content.count("Starts with a birth star sound.") == 1


def test_llm_ideas_still_work_with_a_star(board, monkeypatch):
    idea = NameIdea(
        name="Tilak", native_script="", meaning="A mark", origin="Sanskrit", respelling="TI-lak", speech_lang="hi-IN",
        gender="boy", rarity="uncommon", style="classic", genres=[], insight="", surname_fit="",
    )  # fmt: skip
    seen = {}

    class Fake:
        def suggest(self, request):
            seen.update(request.brief)
            return [idea]

    monkeypatch.setattr(services, "get_ai", Fake)
    board.birth_star = STAR
    board.save()
    assert [name.text for name in services.suggest_names(board)] == ["Tilak"]
    assert seen["birth_sounds"] == STAR["letters"]
    assert CatalogName.objects.filter(key="tilak").exists()


NUMBERS_ON = {"surname": "Rao", "numerology": True}


def number_url(board, name):
    return reverse("number", args=[board.parent_token, name.pk])


@pytest.fixture
def numbered(board):
    board.brief = NUMBERS_ON
    board.save()
    return board


@override_settings(**SOOTA_ON)
def test_a_card_gets_the_number_for_its_spelling(client, numbered, api):
    name = Name.objects.create(board=numbered, text="Tilak", key="tilak")
    page = client.get(reverse("board", args=[numbered.parent_token])).content.decode()
    assert "Working out its number" in page
    assert not api.calls  # The page doesn't wait on Soota.
    response = client.get(number_url(numbered, name), **HTMX)
    assert b"Name number 2 (Moon)." in response.content
    name.refresh_from_db()
    assert name.numerology == {"text": "Tilak", "total": 11, "number": 2, "planet": "Moon"}
    assert not Job.objects.filter(board=numbered).exists()


@override_settings(**SOOTA_ON)
def test_only_the_name_is_sent_for_a_number(client, numbered, api):
    numbered.birth_star = STAR
    numbered.save()
    name = Name.objects.create(board=numbered, text="Tilak", key="tilak")
    client.get(number_url(numbered, name), **HTMX)
    assert api.calls[0]["json"] == {"names": ["Tilak"], "system": "chaldean"}


@override_settings(**SOOTA_ON)
def test_one_request_covers_every_name_waiting(client, numbered, api):
    first = Name.objects.create(board=numbered, text="Tilak", key="tilak")
    Name.objects.create(board=numbered, text="Gauri", key="gauri")
    Name.objects.create(board=numbered, text="Binned", key="binned", status=Name.Status.PASSED)
    client.get(number_url(numbered, first), **HTMX)
    assert [call["json"]["names"] for call in api.calls] == [["Tilak", "Gauri"]]


@override_settings(**SOOTA_ON)
def test_another_spelling_asks_again(numbered, api):
    name = Name.objects.create(board=numbered, text="Tilak", key="tilak", variants=["Thilak"])
    services.request_name_number(name)
    services.make_official(name, "Thilak")
    assert name.current_number is None and name.needs_number
    services.request_name_number(name)
    assert name.current_number["text"] == "Thilak"


@override_settings(**SOOTA_ON)
def test_it_says_when_a_number_matches_the_baby(client, numbered, api):
    numbered.birth_star = {**STAR, "birth_number": 2, "destiny_number": 2}
    numbered.save()
    name = Name.objects.create(board=numbered, text="Tilak", key="tilak")
    content = client.get(number_url(numbered, name), **HTMX).content
    assert b"Same as the birth number." in content
    assert b"Same as the destiny number." in content


@override_settings(**SOOTA_ON)
def test_numbers_are_opt_in(client, board, api):
    name = Name.objects.create(board=board, text="Tilak", key="tilak")
    assert b"its number" not in client.get(reverse("board", args=[board.parent_token])).content
    assert client.get(number_url(board, name), **HTMX).content.strip() == b""
    assert not api.calls


@override_settings(**SOOTA_ON)
def test_the_brief_offers_numerology_and_keeps_the_choice_from_the_llm(client, board):
    assert b'name="numerology"' in client.get(reverse("board", args=[board.parent_token])).content
    client.post(reverse("brief", args=[board.parent_token]), {"surname": "Rao", "numerology": "on"})
    board.refresh_from_db()
    assert board.brief["numerology"] is True
    assert "numerology" not in brief_for_llm(board.brief)


def test_the_brief_doesnt_offer_it_without_soota(client, board):
    assert b'name="numerology"' not in client.get(reverse("board", args=[board.parent_token])).content


@pytest.mark.parametrize("text", ["तिलक", "Tílak", "Ti2lak"])
@override_settings(**SOOTA_ON)
def test_names_without_a_latin_spelling_wait(client, numbered, api, text):
    name = Name.objects.create(board=numbered, text=text, key="tilak")
    assert not name.needs_number
    assert b"its number" not in client.get(number_url(numbered, name), **HTMX).content
    assert not api.calls


@override_settings(**SOOTA_ON)
def test_when_soota_fails_cards_stop_asking_and_show_nothing(client, numbered, monkeypatch):
    monkeypatch.setattr(soota.httpx, "post", FakeSoota(status=503, body={"code": "engine_busy"}))
    name = Name.objects.create(board=numbered, text="Tilak", key="tilak")
    assert client.get(number_url(numbered, name), **HTMX).content.strip() == b""
    name.refresh_from_db()
    assert name.numerology == {}


@override_settings(**SOOTA_ON)
def test_a_mismatched_answer_is_not_stored(numbered, monkeypatch):
    row = {"name": "Other", "total": 1, "number": 1, "planet": "Sun"}
    wrong = {"system": "chaldean", "names": [row], "birth": None}
    monkeypatch.setattr(soota.httpx, "post", FakeSoota(body=wrong))
    name = Name.objects.create(board=numbered, text="Tilak", key="tilak")
    assert services.request_name_number(name) is False
    name.refresh_from_db()
    assert name.numerology == {}


@override_settings(**SOOTA_ON)
def test_relatives_dont_get_numbers(numbered, api):
    numbered.birth_star = STAR
    numbered.save()
    name = Name.objects.create(board=numbered, text="Tilak", key="tilak", status=Name.Status.KEPT)
    guest = Client()
    assert b"its number" not in guest.get(reverse("board", args=[numbered.guest_token])).content
    assert guest.get(number_url_for_guest(numbered, name), **HTMX).status_code == 404


def number_url_for_guest(board, name):
    return reverse("number", args=[board.guest_token, name.pk])
