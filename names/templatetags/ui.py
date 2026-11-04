from django import template
from django.utils.html import format_html

register = template.Library()

TINTS = 6
MOST_DOTS = 8
# Each root is shown by a letter of its own script, so the choice reads before its label does.
SCRIPT_LETTERS = {
    "Sanskrit": "स",
    "Hindi": "ह",
    "Tamil": "த",
    "Telugu": "తె",
    "Kannada": "ಕ",
    "Malayalam": "മ",
    "Marathi": "म",
    "Bengali": "ব",
    "Gujarati": "ગ",
    "Punjabi": "ਪ",
    "Urdu": "ا",
    "Global": "A",
}


@register.simple_tag
def icon(name: str) -> str:
    """Icons sit in a sprite in the base template, so swapped-in fragments can use them too."""
    return format_html('<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-{}"></use></svg>', name)


@register.filter
def tint(key: str) -> str:
    """A name keeps its colour wherever it appears, so people learn to spot it."""
    weighted = sum(position * ord(letter) for position, letter in enumerate(str(key), start=1))
    return f"tint-{weighted % TINTS}"


@register.filter
def times(count: int) -> range:
    return range(min(count, MOST_DOTS))


@register.filter
def script_letter(origin: str) -> str:
    return SCRIPT_LETTERS.get(str(origin), str(origin)[:1])
