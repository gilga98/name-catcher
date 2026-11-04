from datetime import date, time

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from names import places, services
from names.accounts import record_consent
from names.forms import BirthStarForm

pytestmark = pytest.mark.django_db
SOOTA_ON = {"SOOTA_API_URL": "https://soota.test"}
HTMX = {"HTTP_HX_REQUEST": "true"}


def row(name, admin1="Uttar Pradesh", country="India", code="IN", **extra):
    return {
        "name": name, "admin1": admin1, "country": country, "country_code": code, "feature_code": "PPL",
        "latitude": 28.0, "longitude": 79.0, "timezone": "Asia/Kolkata", **extra,
    }  # fmt: skip


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


@pytest.fixture
def open_meteo(monkeypatch):
    """Answers by what's asked for, and records it."""

    class Fake:
        answers: dict = {}
        asked: list = []
        error = None

        def __call__(self, url, params=None, **kwargs):
            self.asked.append(params["name"])
            self.kwargs = kwargs
            if self.error:
                raise self.error
            body = {"results": self.answers.get(params["name"], [])}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    fake = Fake()
    fake.answers, fake.asked = {}, []
    monkeypatch.setattr(places.httpx, "get", fake)
    return fake


def labels(options):
    return [", ".join(filter(None, (option["name"], option["detail"]))) for option in options]


def test_the_same_town_appears_once_however_it_is_spelled(open_meteo):
    open_meteo.answers["Rampur"] = [
        row("Rāmpur", admin2="Rampur"),
        row("Rampur", admin2="Rampur", latitude=28.01),
        row("RAMPUR", admin2="Other name", latitude=28.1),
    ]
    assert labels(places.search("Rampur")) == ["Rāmpur, Uttar Pradesh, India"]


def test_two_towns_with_one_name_are_told_apart_by_district(open_meteo):
    open_meteo.answers["Rampur"] = [
        row("Rāmpur", admin2="Rampur"),
        row("Rāmpur", admin2="Sahāranpur District", latitude=29.8, longitude=77.4),
    ]
    assert labels(places.search("Rampur")) == [
        "Rāmpur, Uttar Pradesh, India",
        "Rāmpur, Sahāranpur District, Uttar Pradesh, India",
    ]


def test_towns_with_one_name_in_different_states_need_no_district(open_meteo):
    open_meteo.answers["Rampur"] = [row("Rampur", admin2="A"), row("Rampur", "Odisha", admin2="B", latitude=21)]
    assert labels(places.search("Rampur")) == ["Rampur, Uttar Pradesh, India", "Rampur, Odisha, India"]


def test_no_two_options_ever_read_the_same(open_meteo):
    rows = [row("Rampur", admin2=f"District {n}", latitude=20 + n) for n in range(5)]
    rows += [row("Rampur", admin2="District 1", latitude=40), row("Rampur", latitude=50), row("Rampur", latitude=60)]
    open_meteo.answers["Rampur"] = rows
    read = labels(places.search("Rampur"))
    assert len(read) == len(set(read))


@pytest.mark.parametrize("code", ["MT", "AIRP", "AIRH", "ADM1", "PCLI", "", None])
def test_only_towns_and_cities_are_offered(open_meteo, code):
    open_meteo.answers["Rampur"] = [row("Rampur", feature_code=code)]
    assert places.search("Rampur") == []


@pytest.mark.parametrize("code", ["PPL", "PPLA", "PPLA2", "PPLC", "PPLX"])
def test_all_kinds_of_town_are_offered(open_meteo, code):
    open_meteo.answers["Rampur"] = [row("Rampur", feature_code=code)]
    assert len(places.search("Rampur")) == 1


@pytest.mark.parametrize("zone", [None, "", "Not/AZone", "../../etc/passwd"])
def test_a_place_without_a_real_time_zone_is_never_offered(open_meteo, zone):
    open_meteo.answers["Rampur"] = [row("Rampur", timezone=zone)]
    assert places.search("Rampur") == []


@pytest.mark.parametrize("field", ["latitude", "longitude"])
def test_a_place_without_coordinates_is_never_offered(open_meteo, field):
    open_meteo.answers["Rampur"] = [row("Rampur", **{field: None})]
    assert places.search("Rampur") == []


def test_india_comes_first_and_exact_names_before_partial_ones(open_meteo):
    open_meteo.answers["Kochi"] = [
        row("Kochikode"),
        row("Kochi", "Kochi", "Japan", "JP", timezone="Asia/Tokyo", latitude=33),
        row("Kochi", "Kerala", latitude=9.9),
    ]
    assert labels(places.search("Kochi")) == [
        "Kochi, Kerala, India",
        "Kochi, Kochi, Japan",
        "Kochikode, Uttar Pradesh, India",
    ]


def test_a_birth_abroad_can_be_found(open_meteo):
    open_meteo.answers["Paris"] = [row("Paris", "Île-de-France", "France", "FR", timezone="Europe/Paris")]
    assert places.search("Paris")[0]["timezone"] == "Europe/Paris"


@pytest.mark.parametrize(
    "former,current,state",
    [("Bombay", "Mumbai", "Maharashtra"), ("bangalore", "Bengaluru", "Karnataka"), ("MADRAS", "Chennai", "Tamil Nadu")],
)
def test_former_names_lead_to_the_city_in_india(open_meteo, former, current, state):
    open_meteo.answers[current] = [row(current, state), row(current, "Elsewhere", "Fiji", "FJ", latitude=1)]
    open_meteo.answers[former] = [row(former, "Nowhere", "Abroad", "US", timezone="America/New_York", latitude=42)]
    found = places.search(former)
    assert labels(found)[0] == f"{current}, {state}, India"
    assert "Fiji" not in " ".join(labels(found))
    assert len(found) == 2  # The namesake abroad is still there, for someone really born in it.


def test_short_queries_cost_nothing(open_meteo):
    assert places.search(" a ") == []
    assert open_meteo.asked == []


def test_searches_are_cached_whatever_the_case_or_spacing(open_meteo):
    open_meteo.answers["Pune"] = [row("Pune")]
    places.search("Pune")
    places.search(" PUNE ")
    assert open_meteo.asked == ["Pune"]


def test_a_failed_search_is_not_remembered_as_no_results(open_meteo):
    open_meteo.error = httpx.ConnectTimeout("too slow")
    with pytest.raises(places.PlaceSearchError):
        places.search("Pune")
    open_meteo.error = None
    open_meteo.answers["Pune"] = [row("Pune")]
    assert len(places.search("Pune")) == 1


def test_it_ignores_proxy_settings_from_the_environment(open_meteo):
    places.search("Pune")
    assert open_meteo.kwargs["trust_env"] is False


@pytest.mark.parametrize(
    "zone,day,clock,hours",
    [
        ("Asia/Kolkata", date(2026, 1, 1), time(6, 30), 5.5),
        ("Asia/Kathmandu", date(2026, 1, 1), time(6, 30), 5.75),
        ("America/New_York", date(1990, 7, 1), time(12), -4.0),  # Daylight saving
        ("America/New_York", date(1990, 1, 1), time(12), -5.0),
        ("Europe/London", date(2026, 6, 1), time(12), 1.0),
        ("Australia/Sydney", date(2026, 1, 1), time(12), 11.0),
    ],
)
def test_the_offset_is_for_that_place_on_that_date(zone, day, clock, hours):
    assert places.utc_offset_hours(day, clock, {"timezone": zone}) == hours


NEW_YORK = {"label": "New York, United States", "latitude": 40.71, "longitude": -74.0, "timezone": "America/New_York"}


def form(**fields):
    return BirthStarForm(
        {"birth_date": "1990-07-01", "birth_time": "12:00", "place": places.sign(NEW_YORK), "consent": "on", **fields}
    )


def test_the_form_sends_soota_the_place_and_its_offset():
    birth = form()
    assert birth.is_valid()
    assert birth.birth_input() == {
        "dob": "1990-07-01", "tob": "12:00:00", "latitude": 40.71, "longitude": -74.0, "timezone_offset": -4.0,
        "ayanamsha": 2, "use_true_node": False,
    }  # fmt: skip


@pytest.mark.parametrize("value", ["", "delhi", "x:y:z", "28.6,77.2"])
def test_only_a_place_from_a_search_is_accepted(value):
    birth = form(place=value)
    assert not birth.is_valid()
    assert "from the list" in birth.errors["place"][0]


def test_a_tampered_place_is_refused():
    signed = places.sign(NEW_YORK)
    assert not form(place=signed[:-2] + ("aa" if signed[-2:] != "aa" else "bb")).is_valid()


def test_a_value_signed_for_something_else_is_refused():
    from django.core import signing

    assert not form(place=signing.dumps(NEW_YORK)).is_valid()


def test_the_future_is_judged_by_the_time_at_that_place():
    samoa = {"label": "Apia, Samoa", "latitude": -13.8, "longitude": -171.8, "timezone": "Pacific/Apia"}
    assert form(birth_date="2020-01-01", place=places.sign(samoa)).is_valid()
    assert "after the birth" in form(birth_date="2999-01-01").errors["birth_date"][0]


def test_a_chosen_place_survives_another_field_being_wrong():
    birth = form(birth_date="soon")
    assert not birth.is_valid()
    assert birth.chosen_place()["label"] == NEW_YORK["label"]
    assert BirthStarForm({"place": "delhi"}).chosen_place() is None


@pytest.fixture
def parent():
    user = get_user_model().objects.create_user(username="p@example.com", email="p@example.com", password="x")
    record_consent(user)
    return user


@pytest.fixture
def board(parent):
    return services.create_board(parent, {})


@pytest.fixture
def client(parent):
    client = Client()
    client.force_login(parent)
    return client


@override_settings(**SOOTA_ON)
def test_the_search_takes_a_post_so_a_birth_place_stays_out_of_urls_and_logs(client, board, open_meteo):
    url = reverse("place_search", args=[board.parent_token])
    assert client.get(url, {"q": "Pune"}).status_code == 405
    open_meteo.answers["Pune"] = [row("Pune", "Maharashtra")]
    found = client.post(url, {"q": "Pune"}, **HTMX).json()["places"]
    assert [(p["name"], p["detail"], p["timezone"]) for p in found] == [("Pune", "Maharashtra, India", "Asia/Kolkata")]
    assert places.read(found[0]["value"])["latitude"] == 28.0


@override_settings(**SOOTA_ON)
def test_a_failed_search_says_so_rather_than_looking_like_no_results(client, board, open_meteo):
    open_meteo.error = httpx.ConnectError("down")
    response = client.post(reverse("place_search", args=[board.parent_token]), {"q": "Pune"}, **HTMX)
    assert response.status_code == 503


def test_the_search_is_closed_until_soota_is_configured(client, board, open_meteo):
    assert client.post(reverse("place_search", args=[board.parent_token]), {"q": "Pune"}).status_code == 404
    assert open_meteo.asked == []


@override_settings(**SOOTA_ON)
def test_relatives_and_strangers_cant_use_the_search(board, open_meteo):
    url = reverse("place_search", args=[board.guest_token])
    assert Client().post(url, {"q": "Pune"}, **HTMX).status_code == 404
    assert open_meteo.asked == []


@override_settings(**SOOTA_ON, PLACE_SEARCH_RATE="1/h")
def test_the_panel_has_the_widget_and_no_fixed_list(client, board):
    content = client.get(reverse("board", args=[board.parent_token])).content.decode()
    assert 'role="combobox"' in content
    assert "birth_place.js" in content
    assert "<select" not in content.split('id="birth-star"')[1].split("</form>")[0]


@override_settings(**SOOTA_ON)
def test_the_chosen_place_comes_back_when_the_form_is_shown_again(client, board):
    response = client.post(
        reverse("read_birth_star", args=[board.parent_token]),
        {"birth_date": "soon", "birth_time": "12:00", "place": places.sign(NEW_YORK), "consent": "on"},
        **HTMX,
    )
    assert b'value="New York, United States"' in response.content
    assert b"Time zone America/New_York" in response.content
