from django import template

register = template.Library()


@register.filter
def starts_with_sound(key: str, sounds: list[str]) -> bool:
    return key.startswith(tuple(sounds))
