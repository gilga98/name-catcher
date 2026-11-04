from types import SimpleNamespace

import pytest

from names.dedup import find_duplicate, phonetic_key


def candidate(text):
    return SimpleNamespace(text=text, key=phonetic_key(text))


@pytest.mark.parametrize(
    "variant",
    ["Aarav", "Arav", "Aaravv", "आरव", "ಆರವ", "ஆரவ்", "ఆరవ్", "આરવ", "Āarav"],
)
def test_spellings_and_scripts_share_a_key(variant):
    assert phonetic_key(variant) == "arav"


@pytest.mark.parametrize(("a", "b"), [("Shiva", "Siva"), ("Rishi", "Rishee"), ("Pooja", "Puja"), ("Bharat", "Barat")])
def test_sound_alike_spellings_share_a_key(a, b):
    assert phonetic_key(a) == phonetic_key(b)


def test_same_name_in_another_script_is_found():
    assert find_duplicate("आरव", [candidate("Aarav")]).text == "Aarav"


def test_close_spelling_is_offered_for_confirmation():
    assert find_duplicate("Arjunn", [candidate("Arjuna")]).text == "Arjuna"


def test_unrelated_names_are_left_alone():
    assert find_duplicate("Meera", [candidate("Rohan")]) is None
