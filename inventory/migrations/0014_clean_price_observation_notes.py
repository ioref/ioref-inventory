from django.db import migrations

IMPORT_NOTES = {
    "imported",
    "Imported from FINAL Inventory - Phys Comp Lab(2).csv.",
}


def clean_price_observation_notes(apps, schema_editor):
    PriceObservation = apps.get_model("inventory", "PriceObservation")

    PriceObservation.objects.filter(note__in=IMPORT_NOTES).update(note="")

    for observation in PriceObservation.objects.filter(
        purchase_link="",
        note__startswith="http",
    ):
        url = observation.note.strip()
        if not url.startswith(("http://", "https://")):
            continue
        observation.purchase_link = url
        observation.note = ""
        observation.save(update_fields=("purchase_link", "note"))


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0013_price_observation_observed_at_default"),
    ]

    operations = [
        migrations.RunPython(
            clean_price_observation_notes,
            migrations.RunPython.noop,
        )
    ]
