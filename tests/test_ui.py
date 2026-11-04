import pytest
from django.urls import reverse

from names.forms import ORIGINS
from names.templatetags.ui import SCRIPT_LETTERS, TINTS, icon, script_letter, times, tint


def test_a_name_keeps_its_colour():
    assert tint("ahana") == tint("ahana")
    assert int(tint("ಅಹನಾ").split("-")[1]) in range(TINTS)


def test_every_root_has_a_letter_of_its_own():
    assert {origin for origin, _ in ORIGINS} == set(SCRIPT_LETTERS)
    assert script_letter("Kannada") == "ಕ"


def test_a_long_name_does_not_draw_endless_syllable_dots():
    assert len(times(40)) < 40


def test_icons_escape_their_name():
    assert "<b>" not in icon("<b>")


@pytest.mark.django_db
def test_the_brief_works_as_one_form_without_scripts(client):
    html = client.get(reverse("home")).content.decode()
    assert "data-wizard" in html
    assert html.count("data-step") == 6
    assert 'type="submit"' in html


@pytest.mark.django_db
def test_only_a_missing_static_file_is_kept_out_of_caches(client):
    missing = client.get("/static/names/app.0000000000.js")
    assert missing.status_code == 404
    assert missing["Cache-Control"] == "no-store"
    assert "Cache-Control" not in client.get("/no-such-page")


@pytest.mark.django_db
def test_a_missed_hashed_asset_falls_back_to_unhashed(client):
    fallback = client.get("/static/names/app.000000000000.css")
    assert fallback.status_code == 200
    assert fallback["Cache-Control"] == "no-cache"


@pytest.mark.django_db
def test_a_truly_missing_hashed_asset_is_kept_out_of_caches(client):
    missing = client.get("/static/names/nonexistent.000000000000.css")
    assert missing.status_code == 404
    assert missing["Cache-Control"] == "no-store"
