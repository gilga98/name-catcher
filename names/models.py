import re
import secrets

from django.conf import settings
from django.db import models

_VOWEL_GROUP = re.compile(r"[aeiouy]+")


def new_token() -> str:
    return secrets.token_urlsafe(16)


class Board(models.Model):
    """One family's naming project. Parents sign in; the family link alone lets relatives vote and suggest."""

    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="boards")
    parent_token = models.CharField(max_length=32, unique=True, default=new_token)
    guest_token = models.CharField(max_length=32, unique=True, default=new_token)
    invite_token = models.CharField(max_length=32, unique=True, default=new_token)
    brief = models.JSONField(default=dict)
    family_ideas = models.JSONField(default=dict, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)


class Name(models.Model):
    class Status(models.TextChoices):
        REVIEW = "review"
        KEPT = "kept"
        PASSED = "passed"

    class Source(models.TextChoices):
        FAMILY = "family"
        CATALOG = "catalog"
        LLM = "llm"

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="names")
    text = models.CharField(max_length=60)
    key = models.CharField(max_length=60, db_index=True)
    native = models.CharField(max_length=60, blank=True)
    meaning = models.CharField(max_length=300, blank=True)
    origin = models.CharField(max_length=80, blank=True)
    respelling = models.CharField(max_length=80, blank=True)
    speech_lang = models.CharField(max_length=20, blank=True)
    surname_fit = models.CharField(max_length=200, blank=True)
    insight = models.CharField(max_length=200, blank=True)
    tags = models.JSONField(default=list)
    variants = models.JSONField(default=list)
    status = models.CharField(max_length=10, choices=Status, default=Status.REVIEW)
    source = models.CharField(max_length=10, choices=Source, default=Source.FAMILY)
    rank = models.PositiveIntegerField(default=0)
    suggested_by = models.CharField(max_length=40, blank=True)
    audio = models.BinaryField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rank", "created_at"]

    @property
    def both_parents_love(self) -> bool:
        return sum(vote.is_parent for vote in self.votes.all()) >= 2

    @property
    def syllables(self) -> int:
        spoken = self.respelling or self.text
        beats = spoken.count("-") + 1 if "-" in spoken else len(_VOWEL_GROUP.findall(spoken.lower()))
        return max(beats, 1)

    def settle(self) -> None:
        """Binned only when every parent has rejected it. Any parent's yes keeps it alive."""
        swipes = list(self.swipes.all())
        if not swipes:
            return
        rejected = sum(not swipe.liked for swipe in swipes)
        if rejected >= max(self.board.members.count(), 1):
            status = self.Status.PASSED
        elif rejected < len(swipes):
            status = self.Status.KEPT
        else:
            status = self.Status.REVIEW
        if status != self.status:
            self.status = status
            self.save(update_fields=["status"])


class Swipe(models.Model):
    """One parent's private decision on a name. Partners don't see it until they've decided too."""

    name = models.ForeignKey(Name, on_delete=models.CASCADE, related_name="swipes")
    voter = models.CharField(max_length=32)
    liked = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name", "voter"], name="one_swipe_per_parent")]


class Vote(models.Model):
    name = models.ForeignKey(Name, on_delete=models.CASCADE, related_name="votes")
    voter = models.CharField(max_length=32)
    voter_name = models.CharField(max_length=40, blank=True)
    is_parent = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name", "voter"], name="one_vote_per_voter")]


class Profile(models.Model):
    """What a person controls about themselves. Plans and billing will hang off the user, not this row."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    display_name = models.CharField(max_length=40, blank=True)
    ai_enabled = models.BooleanField(default=True)


class Genre(models.Model):
    """A tag people can look for, like Nature or Vedic. Admins manage the list; contributors pick from it."""

    name = models.CharField(max_length=40, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class CatalogName(models.Model):
    """The shared, human-moderated pool. Every name served from here is an LLM call we didn't make."""

    class Status(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"

    class Source(models.TextChoices):
        ADMIN = "admin"
        USER = "user"
        LLM = "llm"

    class Gender(models.TextChoices):
        ANY = "any"
        GIRL = "girl"
        BOY = "boy"
        NEUTRAL = "neutral"

    class Rarity(models.TextChoices):
        FAMILIAR = "familiar"
        UNCOMMON = "uncommon"
        RARE = "rare"

    class Style(models.TextChoices):
        ANY = "any"
        CLASSIC = "classic"
        MODERN = "modern"

    text = models.CharField(max_length=60)
    key = models.CharField(max_length=60, unique=True)
    native = models.CharField(max_length=60, blank=True)
    meaning = models.CharField(max_length=300, blank=True)
    insight = models.CharField(max_length=200, blank=True)
    origin = models.CharField(max_length=80, blank=True, db_index=True)
    respelling = models.CharField(max_length=80, blank=True)
    speech_lang = models.CharField(max_length=20, blank=True)
    gender = models.CharField(max_length=10, choices=Gender, default=Gender.ANY)
    rarity = models.CharField(max_length=10, choices=Rarity, default=Rarity.UNCOMMON)
    style = models.CharField(max_length=10, choices=Style, default=Style.ANY)
    genres = models.ManyToManyField(Genre, blank=True, related_name="names")
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING, db_index=True)
    source = models.CharField(max_length=10, choices=Source, default=Source.ADMIN)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    submissions = models.PositiveIntegerField(default=1)  # How many people independently offered this name.
    picks = models.PositiveIntegerField(default=0)  # How often it reached a board. High means overused.
    audio = models.BinaryField(null=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["text"]

    def __str__(self) -> str:
        return self.text


class ConsentRecord(models.Model):
    """Append-only proof of what was agreed to. Holds no contact details, so it can outlive the account."""

    class Purpose(models.TextChoices):
        TERMS = "terms"
        CONTRIBUTION = "contribution"

    class Action(models.TextChoices):
        GIVEN = "given"
        WITHDRAWN = "withdrawn"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="consents")
    purpose = models.CharField(max_length=20, choices=Purpose)
    action = models.CharField(max_length=10, choices=Action, default=Action.GIVEN)
    policy_version = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class AICall(models.Model):
    """One paid LLM request: the basis for quotas now, cost reporting in the admin, and plans later."""

    class Kind(models.TextChoices):
        SUGGEST = "suggest"
        DESCRIBE = "describe"
        SPEAK = "speak"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="ai_calls")
    kind = models.CharField(max_length=10, choices=Kind)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
