"""What the LLM gets to see. The rule: send what improves the names, never what only identifies people.

Sent: preferences, the parents' own description, and names they reacted to. These carry no account or identity.
Sent only if the parent opts in on the brief: the family name, because it changes how a name sounds.
Sent only if a parent adds a birth star, and only to the parents' own suggestions: its naming sounds.
Never sent: email, account IDs, who voted or suggested what, the baby's nickname or photo, or "names to avoid",
which is often real relatives. Those are filtered out on our side instead. Nor the star itself, or birth details.
"""

import re

_SHARED = ("gender", "origins", "themes", "style", "uniqueness", "starts_with")
_CONTACT_DETAILS = (
    re.compile(r"\S+@\S+"),
    re.compile(r"https?://\S+|www\.\S+", re.I),
    re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)"),  # Phone, Aadhaar and other long numbers.
)
MAX_NOTES = 500


def scrub(text: str) -> str:
    """People write what they like in a free-text box. Contact details and ID numbers never help find a name."""
    for pattern in _CONTACT_DETAILS:
        text = pattern.sub("[removed]", text)
    return text[:MAX_NOTES]


def brief_for_llm(brief: dict, birth_star: dict | None = None) -> dict:
    shared = {key: brief[key] for key in _SHARED if brief.get(key)}
    if birth_star and not brief.get("starts_with"):  # A sound the parents picked is a hard limit, and it wins.
        shared["birth_sounds"] = birth_star["letters"]
    if brief.get("notes"):
        shared["notes"] = scrub(brief["notes"])
    if brief.get("share_surname") and brief.get("surname"):
        shared["surname"] = brief["surname"]
    return shared
