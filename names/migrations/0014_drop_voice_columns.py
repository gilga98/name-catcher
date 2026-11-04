from django.db import migrations, models

CHOICES = [
    ("none", "None"), ("pending", "Pending"), ("running", "Running"), ("done", "Done"), ("failed", "Failed"),
]  # fmt: skip


class Migration(migrations.Migration):
    """0013 told the models these were gone, so they're described again here for Django to be able to drop them."""

    dependencies = [
        ("names", "0013_stop_using_voice_columns"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="catalogname", name="audio", field=models.BinaryField(editable=False, null=True)
                ),
                migrations.AddField(
                    model_name="catalogname",
                    name="voice_state",
                    field=models.CharField(
                        choices=CHOICES, db_index=True, default="none", db_default="none", max_length=10
                    ),
                ),
                migrations.AddField(
                    model_name="catalogname",
                    name="voice_attempts",
                    field=models.PositiveSmallIntegerField(default=0, db_default=0),
                ),
                migrations.AddField(
                    model_name="catalogname",
                    name="voice_claimed_at",
                    field=models.DateTimeField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="name", name="audio", field=models.BinaryField(editable=False, null=True)
                ),
            ]
        ),
        migrations.RemoveField(model_name="catalogname", name="audio"),
        migrations.RemoveField(model_name="catalogname", name="voice_state"),
        migrations.RemoveField(model_name="catalogname", name="voice_attempts"),
        migrations.RemoveField(model_name="catalogname", name="voice_claimed_at"),
        migrations.RemoveField(model_name="name", name="audio"),
    ]
