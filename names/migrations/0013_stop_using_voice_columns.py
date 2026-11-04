from django.db import migrations, models

CHOICES = [
    ("none", "None"), ("pending", "Pending"), ("running", "Running"), ("done", "Done"), ("failed", "Failed"),
]  # fmt: skip


class Migration(migrations.Migration):
    """Models forget these columns here, and 0014 drops them, so containers on either side of a deploy keep working.
    The database fills in the two required columns until then, as code from now on won't."""

    dependencies = [
        ("names", "0012_add_sounds_like"),
    ]

    operations = [
        migrations.AlterField(
            model_name="catalogname",
            name="voice_state",
            field=models.CharField(choices=CHOICES, db_index=True, default="none", db_default="none", max_length=10),
        ),
        migrations.AlterField(
            model_name="catalogname",
            name="voice_attempts",
            field=models.PositiveSmallIntegerField(default=0, db_default=0),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="catalogname", name="audio"),
                migrations.RemoveField(model_name="catalogname", name="voice_state"),
                migrations.RemoveField(model_name="catalogname", name="voice_attempts"),
                migrations.RemoveField(model_name="catalogname", name="voice_claimed_at"),
                migrations.RemoveField(model_name="name", name="audio"),
            ]
        ),
    ]
