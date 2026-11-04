from django import forms
from django.core.validators import RegexValidator

GENDERS = [("any", "Open to any"), ("girl", "Girl"), ("boy", "Boy"), ("neutral", "Gender-neutral")]
ORIGINS = [
    (origin, origin)
    for origin in (
        "Sanskrit", "Hindi", "Tamil", "Telugu", "Kannada", "Malayalam", "Marathi",
        "Bengali", "Gujarati", "Punjabi", "Urdu", "Global",
    )
]  # fmt: skip
THEMES = [
    (theme, theme)
    for theme in ("Nature", "Light", "Strength", "Wisdom", "Peace", "Joy", "Devotion", "Love")
]
STYLES = [("any", "Either"), ("classic", "Classic"), ("modern", "Modern")]


class BriefForm(forms.Form):
    surname = forms.CharField(max_length=40, required=False)
    gender = forms.ChoiceField(choices=GENDERS, widget=forms.RadioSelect, initial="any", required=False)
    origins = forms.MultipleChoiceField(choices=ORIGINS, widget=forms.CheckboxSelectMultiple, required=False)
    themes = forms.MultipleChoiceField(choices=THEMES, widget=forms.CheckboxSelectMultiple, required=False)
    style = forms.ChoiceField(choices=STYLES, widget=forms.RadioSelect, initial="any", required=False)
    starts_with = forms.CharField(max_length=10, required=False)
    avoid = forms.CharField(max_length=200, required=False)


class AddNameForm(forms.Form):
    text = forms.CharField(
        max_length=40,
        strip=True,
        validators=[RegexValidator(r"[\d<>/:@]", "Enter just the name.", inverse_match=True)],
    )
    by = forms.CharField(max_length=40, required=False, strip=True)
