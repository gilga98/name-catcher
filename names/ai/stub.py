"""Offline backend for tests and for working on the UI without spending API quota."""


from .base import DescribedIdea, NameIdea, RespelledName, SuggestionRequest

_IDEAS = (
    ("Aarav", "आरव", "Peaceful, a calm and gentle sound", "Sanskrit", "AA-ruhv", "hi-IN"),
    ("Anaya", "अनया", "Caring, full of grace", "Sanskrit", "uh-NAA-yaa", "hi-IN"),
    ("Ishaan", "ईशान", "The sun, guardian of the north-east", "Sanskrit", "ee-SHAAN", "hi-IN"),
    ("Meera", "मीरा", "Devoted, after the poet-saint", "Sanskrit", "MEE-raa", "hi-IN"),
    ("Nila", "நிலா", "The moon", "Tamil", "NI-laa", "ta-IN"),
    ("Kavya", "ಕಾವ್ಯ", "Poetry", "Kannada", "KAAV-yuh", "kn-IN"),
    ("Rohan", "रोहन", "Ascending, rising", "Sanskrit", "ROH-hun", "hi-IN"),
    ("Tara", "तारा", "Star", "Sanskrit", "TAA-raa", "hi-IN"),
)


def _idea(row: tuple) -> NameIdea:
    name, native, meaning, origin, respelling, lang = row
    return NameIdea(
        name=name,
        native_script=native,
        meaning=meaning,
        origin=origin,
        respelling=respelling,
        sounds_like=name.lower(),
        speech_lang=lang,
        gender="any",
        rarity="uncommon",
        style="any",
        genres=[],
        insight="",
        surname_fit="",
    )


class StubAI:
    def suggest(self, request: SuggestionRequest) -> list[NameIdea]:
        seen = {*request.liked, *request.passed, *request.shown}
        fresh = [row for row in _IDEAS if row[0] not in seen]
        return [_idea(row) for row in fresh[:4]]

    def describe_many(self, names: list[str], genres: list[str]) -> list[DescribedIdea]:
        described = (_idea((name, name, "A name your family suggested", "", name, "en-IN")) for name in names)
        return [DescribedIdea(**idea.model_dump(), given=idea.name) for idea in described]

    def respell_many(self, names: list[str]) -> list[RespelledName]:
        return [RespelledName(given=name, sounds_like=name.lower()) for name in names]
