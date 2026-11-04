from django.db import migrations

GENRES = (
    "Nature", "Light", "Strength", "Wisdom", "Peace", "Joy", "Devotion", "Love",
    "Mythology", "Literature", "Vedic", "Celestial",
)  # fmt: skip


def seed(apps, schema_editor):
    Genre = apps.get_model("names", "Genre")
    Genre.objects.bulk_create([Genre(name=name) for name in GENRES], ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("names", "0003_add_catalog_consent_and_usage")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
