import secrets

from django.db import models


def new_token() -> str:
    return secrets.token_urlsafe(16)


class Board(models.Model):
    """One family's naming project. Each link is a capability: holding it grants access."""

    parent_token = models.CharField(max_length=32, unique=True, default=new_token)
    guest_token = models.CharField(max_length=32, unique=True, default=new_token)
    brief = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class Name(models.Model):
    class Status(models.TextChoices):
        REVIEW = "review"
        KEPT = "kept"
        PASSED = "passed"

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="names")
    text = models.CharField(max_length=60)
    key = models.CharField(max_length=60, db_index=True)
    native = models.CharField(max_length=60, blank=True)
    meaning = models.CharField(max_length=300, blank=True)
    origin = models.CharField(max_length=80, blank=True)
    respelling = models.CharField(max_length=80, blank=True)
    speech_lang = models.CharField(max_length=20, blank=True)
    surname_fit = models.CharField(max_length=200, blank=True)
    variants = models.JSONField(default=list)
    status = models.CharField(max_length=10, choices=Status, default=Status.REVIEW)
    rank = models.PositiveIntegerField(default=0)
    suggested_by = models.CharField(max_length=40, blank=True)
    audio = models.BinaryField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rank", "created_at"]

    @property
    def both_parents_love(self) -> bool:
        return sum(vote.is_parent for vote in self.votes.all()) >= 2


class Vote(models.Model):
    name = models.ForeignKey(Name, on_delete=models.CASCADE, related_name="votes")
    voter = models.CharField(max_length=32)
    voter_name = models.CharField(max_length=40, blank=True)
    is_parent = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name", "voter"], name="one_vote_per_voter")]
