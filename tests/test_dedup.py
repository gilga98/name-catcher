from types import SimpleNamespace

import pytest

from names.ai.base import AIError
from names.dedup import find_duplicate, phonetic_key


def candidate(text):
    return SimpleNamespace(text=text, key=phonetic_key(text))


class Judge:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def same_name(self, name_a, name_b):
        self.calls += 1
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


@pytest.mark.parametrize(
    "variant",
    ["Aarav", "Arav", "Aaravv", "आरव", "ಆರವ", "ஆரவ்", "ఆరవ్", "આરવ", "Āarav"],
)
def test_spellings_and_scripts_share_a_key(variant):
    assert phonetic_key(variant) == "arav"


@pytest.mark.parametrize(("a", "b"), [("Shiva", "Siva"), ("Rishi", "Rishee"), ("Pooja", "Puja"), ("Bharat", "Barat")])
def test_sound_alike_spellings_share_a_key(a, b):
    assert phonetic_key(a) == phonetic_key(b)


def test_exact_key_match_needs_no_judge(db):
    judge = Judge(False)
    assert find_duplicate("आरव", [candidate("Aarav")], judge).text == "Aarav"
    assert judge.calls == 0


def test_unrelated_names_never_reach_the_judge(db):
    judge = Judge(True)
    assert find_duplicate("Meera", [candidate("Rohan")], judge) is None
    assert judge.calls == 0


def test_judge_decides_the_uncertain_middle_and_is_cached(db):
    judge = Judge(False)
    assert find_duplicate("Arnav", [candidate("Aarav")], judge) is None
    assert find_duplicate("Arnav", [candidate("Aarav")], judge) is None
    assert judge.calls == 1


def test_unavailable_judge_leaves_it_to_the_person(db):
    assert find_duplicate("Arjunn", [candidate("Arjuna")], Judge(AIError("down"))).text == "Arjuna"
