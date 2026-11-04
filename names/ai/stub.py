"""Offline backend for tests and for working on the UI without spending API quota."""

import io
import wave

from .base import NameIdea, SuggestionRequest

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
        seen = {*request.liked, *request.passed}
        fresh = [row for row in _IDEAS if row[0] not in seen]
        return [_idea(row) for row in fresh[:4]]

    def describe(self, name: str, genres: list[str]) -> NameIdea:
        return _idea((name, name, "A name your family suggested", "", name, "en-IN"))

    def speak(self, text: str) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b"\0\0" * 2400)
        return buffer.getvalue()
