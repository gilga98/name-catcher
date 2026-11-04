from django.db import migrations

from names.dedup import phonetic_key


def refresh(apps, schema_editor):
    """The normaliser improved, so stored keys need recomputing. Answers the LLM already gave stay marked as done."""
    CatalogName = apps.get_model("names", "CatalogName")
    Name = apps.get_model("names", "Name")
    CatalogName.objects.filter(source="llm").exclude(meaning="").update(enrich_state="done")
    for entry in CatalogName.objects.defer("audio"):
        key = phonetic_key(entry.text)[:60]
        if key and key != entry.key and not CatalogName.objects.filter(key=key).exists():
            CatalogName.objects.filter(pk=entry.pk).update(key=key)
    for name in Name.objects.defer("audio"):
        key = phonetic_key(name.text)[:60]
        if key and key != name.key:
            Name.objects.filter(pk=name.pk).update(key=key)


class Migration(migrations.Migration):
    dependencies = [("names", "0006_add_jobs_aliases_and_enrichment")]
    operations = [migrations.RunPython(refresh, migrations.RunPython.noop)]
