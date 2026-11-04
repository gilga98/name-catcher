import unicodedata
from types import SimpleNamespace

import pytest

from names.dedup import find_duplicate, phonetic_key, speech_language


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


@pytest.mark.parametrize(
    "variant",
    ["Krishna", "कृष्ण", "ಕೃಷ್ಣ", "కృష్ణ", "കൃഷ്ണ", "কৃষ্ণ", "કૃષ્ણ", "କୃଷ୍ଣ"],
)
def test_a_final_vowel_after_a_cluster_is_kept(variant):
    assert phonetic_key(variant) == "krisna"


@pytest.mark.parametrize(("a", "b"), [("Lakshmi", "Laxmi"), ("Lakshmi", "ಲಕ್ಷ್ಮಿ"), ("Kavya", "ಕಾವ್ಯ"), ("Ganesh", "ಗಣೇಶ")])
def test_common_indian_variations_share_a_key(a, b):
    assert phonetic_key(a) == phonetic_key(b)


@pytest.mark.parametrize("invisible", ["  ಆರವ ", "ಆ\u200dರವ", "ಆರವ\u200c", "\ufeffಆರವ"])
def test_keyboard_artefacts_do_not_change_the_key(invisible):
    assert phonetic_key(invisible) == "arav"


def test_composed_and_decomposed_text_match():
    assert phonetic_key(unicodedata.normalize("NFD", "ಕೋಮಲ")) == phonetic_key("ಕೋಮಲ")


@pytest.mark.parametrize(
    ("typed", "existing"),
    [("Mohammed", "Muhammad"), ("محمد", "Muhammad"), ("கிருஷ்ணா", "Krishna"), ("Krushna", "Krishna"), ("آرو", "Aarav")],
)
def test_harder_variations_are_offered_to_the_person(typed, existing):
    assert find_duplicate(typed, [candidate(existing)]).text == existing


@pytest.mark.parametrize(("a", "b"), [("Mira", "Mara"), ("Rohan", "Rohit")])
def test_different_names_are_not_offered_as_the_same(a, b):
    assert find_duplicate(a, [candidate(b)]) is None


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("ಆರವ", "kn-IN"), ("आरव", "hi-IN"), ("ஆரவ்", "ta-IN"), ("ఆరవ్", "te-IN"),
        ("ആരവ്", "ml-IN"), ("আরব", "bn-IN"), ("Aarav", ""),
    ],  # fmt: skip
)
def test_script_tells_the_speech_language(text, language):
    assert speech_language(text) == language


def test_unrelated_names_are_left_alone():
    assert find_duplicate("Meera", [candidate("Rohan")]) is None
