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
