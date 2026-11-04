from django import forms
from django.conf import settings
from django.core.validators import RegexValidator
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from . import places, soota, turnstile
from .catalog import themes_from_text
from .dedup import clean as clean_spacing
from .dedup import looks_like_name, phonetic_key
from .models import Genre, Profile

GENDERS = [("any", "Open to any"), ("girl", "Girl"), ("boy", "Boy"), ("neutral", "Gender-neutral")]
ORIGINS = [
    (origin, origin)
    for origin in (
        "Sanskrit", "Hindi", "Tamil", "Telugu", "Kannada", "Malayalam", "Marathi",
        "Bengali", "Gujarati", "Punjabi", "Urdu", "Global",
    )
]  # fmt: skip
STYLES = [("any", "Either"), ("classic", "Classic"), ("modern", "Modern")]
UNIQUENESS = [("any", "Any"), ("familiar", "Familiar"), ("uncommon", "Uncommon"), ("rare", "Rare")]
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
NOT_A_LISTED_PLACE = "Choose your place of birth from the list."


AVATARS = [(f"a{number}", f"Baby picture {number}") for number in range(1, 7)]


def genre_choices() -> list[tuple[str, str]]:
    """Admins manage this list, so a new genre reaches the brief and the contribution form without a deploy."""
    return [(name, name) for name in Genre.objects.filter(active=True).values_list("name", flat=True)]


class BriefForm(forms.Form):
    nickname = forms.CharField(max_length=30, required=False)
    avatar = forms.ChoiceField(choices=AVATARS, widget=forms.RadioSelect, initial="a1", required=False)
    surname = forms.CharField(max_length=40, required=False)
    share_surname = forms.BooleanField(required=False)
    gender = forms.ChoiceField(choices=GENDERS, widget=forms.RadioSelect, initial="any", required=False)
    origins = forms.MultipleChoiceField(choices=ORIGINS, widget=forms.CheckboxSelectMultiple, required=False)
    notes = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 4}))
    share_notes = forms.BooleanField(required=False, initial=True)
    style = forms.ChoiceField(choices=STYLES, widget=forms.RadioSelect, initial="any", required=False)
    uniqueness = forms.ChoiceField(choices=UNIQUENESS, widget=forms.RadioSelect, initial="any", required=False)
    starts_with = forms.CharField(max_length=10, required=False)
    avoid = forms.CharField(max_length=200, required=False)
    numerology = forms.BooleanField(required=False)

    def numerology_available(self) -> bool:
        return bool(settings.SOOTA_API_URL)

    def clean(self):
        cleaned = super().clean()
        cleaned["themes"] = themes_from_text(cleaned.get("notes", ""))
        return cleaned


class BirthStarForm(forms.Form):
    birth_date = forms.DateField()
    birth_time = forms.TimeField()
    place = forms.CharField(error_messages={"required": NOT_A_LISTED_PLACE})
    consent = forms.BooleanField(error_messages={"required": "Tick the box so we can send these details."})

    def clean_place(self) -> dict:
        try:
            return places.read(self.cleaned_data["place"])
        except places.BadPlace as exc:
            raise forms.ValidationError(NOT_A_LISTED_PLACE) from exc

    def clean(self):
        cleaned = super().clean()
        day, clock, place = cleaned.get("birth_date"), cleaned.get("birth_time"), cleaned.get("place")
        if day and clock and place and places.local_moment(day, clock, place) > timezone.now():
            self.add_error("birth_date", "A birth star can only be worked out after the birth.")
        return cleaned

    def chosen_place(self) -> dict | None:
        """So a place already chosen is still chosen when the form comes back because another field was wrong."""
        try:
            return places.read(self.data.get("place", ""))
        except places.BadPlace:
            return None

    def birth_input(self) -> dict:
        data = self.cleaned_data
        return soota.birth_input(data["birth_date"], data["birth_time"], data["place"])


class AddNameForm(forms.Form):
    text = forms.CharField(
        max_length=40,
        strip=True,
        validators=[RegexValidator(r"[\d<>/:@]", "Enter just the name.", inverse_match=True)],
    )
    by = forms.CharField(
        max_length=40,
        required=False,
        strip=True,
        validators=[RegexValidator(r"[\d<>/:@.]", "Enter just your name.", inverse_match=True)],
    )

    def clean_text(self) -> str:
        text = clean_spacing(self.cleaned_data["text"])
        if not phonetic_key(text) or not looks_like_name(text):
            raise forms.ValidationError("Enter just the name.")  # Symbols have no sound; sentences aren't names.
        return text


class SignupConsentForm(forms.Form):
    """allauth mixes this into both the email and the Google sign-up forms, so nobody skips it."""

    accept_terms = forms.BooleanField()
    confirm_adult = forms.BooleanField(label="I am 18 or older.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["accept_terms"].label = format_html(
            'I have read the <a href="{}" target="_blank">Privacy Policy</a> and agree to the '
            '<a href="{}" target="_blank">Terms of Use</a>, and I consent to my data being used as they describe.',
            reverse("privacy"),
            reverse("terms"),
        )
        if settings.TURNSTILE_SECRET_KEY and settings.TURNSTILE_SITE_KEY:
            self.fields["turnstile"] = forms.CharField(required=False, label="", widget=turnstile.TurnstileWidget)

    def clean(self):
        cleaned = super().clean()
        if "turnstile" in self.fields and not turnstile.is_person(self.data.get(turnstile.RESPONSE_FIELD, "")):
            raise forms.ValidationError("We couldn't confirm you're a person. Please try again.")
        return cleaned

    def signup(self, request, user) -> None:
        from .accounts import record_consent

        record_consent(user)


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ["display_name", "ai_enabled"]
        labels = {"display_name": "Your name on boards", "ai_enabled": "Use AI when our catalogue runs out"}


class DeleteAccountForm(forms.Form):
    confirm = forms.CharField(label="Type DELETE to confirm")

    def clean_confirm(self) -> str:
        if self.cleaned_data["confirm"] != "DELETE":
            raise forms.ValidationError("Type DELETE, in capitals, to confirm.")
        return self.cleaned_data["confirm"]


def _upload_text(upload) -> str:
    if upload.size > MAX_UPLOAD_BYTES:
        raise forms.ValidationError("That file is over 2 MB. Split it into smaller files.")
    try:
        return upload.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise forms.ValidationError("Save the file as UTF-8 CSV and try again.") from exc


class ContributeForm(forms.Form):
    names = forms.CharField(
        required=False,
        label="Names, one per line",
        widget=forms.Textarea(attrs={"rows": 6, "placeholder": "Ahana, the first rays of the sun"}),
        help_text="Write each as a name, then a comma and its meaning if you know it.",
    )
    file = forms.FileField(required=False, label="Or upload a CSV")
    genres = forms.MultipleChoiceField(choices=genre_choices, widget=forms.CheckboxSelectMultiple, required=False)
    origin = forms.ChoiceField(choices=[("", "Not sure"), *ORIGINS], required=False)
    gender = forms.ChoiceField(choices=GENDERS, required=False, initial="any")
    rights = forms.BooleanField(
        label="I wrote this list or may share it, and I license it to Naamadeva under the Terms of Use."
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("file"):
            cleaned["names"] = _upload_text(cleaned["file"])
        if not cleaned.get("names") and not self.errors:
            raise forms.ValidationError("Add at least one name, or choose a file.")
        return cleaned


class CatalogUploadForm(forms.Form):
    file = forms.FileField(label="CSV file")
    genres = forms.ModelMultipleChoiceField(
        Genre.objects.all(), required=False, help_text="Applied to every row, on top of any in its genres column."
    )
    publish = forms.BooleanField(required=False, initial=True, label="Publish straight away")

    def clean_file(self):
        return _upload_text(self.cleaned_data["file"])
