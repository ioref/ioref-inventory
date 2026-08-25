import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from django.db import migrations, models

OBSERVED_AT = datetime(2024, 12, 17, 17, 0, tzinfo=timezone.utc)
IMPORT_NOTE = "Imported from FINAL Inventory - Phys Comp Lab(2).csv."
PRECISION_CORRECTIONS = {
    "0875": (Decimal("1.1246"), Decimal("1.12458")),
    "1519": (Decimal("0.0344"), Decimal("0.03444")),
    "1622": (Decimal("0.0272"), Decimal("0.02716")),
    "1626": (Decimal("0.0179"), Decimal("0.01786")),
}


def rows():
    source = Path(__file__).with_name("0009_legacy_procurement.csv")
    with source.open(encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def restore_procurement(apps, schema_editor):
    Part = apps.get_model("inventory", "Part")
    PriceObservation = apps.get_model("inventory", "PriceObservation")

    for row in rows():
        part = Part.objects.filter(part_number=row["part_number"]).first()
        if part is None:
            continue

        PriceObservation.objects.get_or_create(
            part=part,
            price=Decimal(row["price"]),
            observed_at=OBSERVED_AT,
            defaults={
                "currency": "USD",
                "supplier": row["supplier"],
                "purchase_link": row["purchase_link"],
                "note": IMPORT_NOTE,
            },
        )

        if row["unit"] and part.unit == "each":
            part.unit = row["unit"]
            part.save(update_fields=["unit"])

    for part_number, (rounded, exact) in PRECISION_CORRECTIONS.items():
        PriceObservation.objects.filter(
            part__part_number=part_number,
            price=rounded,
            observed_at__date=OBSERVED_AT.date(),
        ).update(price=exact)


def remove_procurement(apps, schema_editor):
    Part = apps.get_model("inventory", "Part")
    PriceObservation = apps.get_model("inventory", "PriceObservation")

    PriceObservation.objects.filter(
        observed_at=OBSERVED_AT,
        note=IMPORT_NOTE,
    ).delete()
    for part_number, (rounded, exact) in PRECISION_CORRECTIONS.items():
        PriceObservation.objects.filter(
            part__part_number=part_number,
            price=exact,
            observed_at__date=OBSERVED_AT.date(),
        ).update(price=rounded)
    Part.objects.filter(part_number="1998", unit="meter").update(unit="each")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0008_seed_ioref_category_assignments")]

    operations = [
        migrations.AlterField(
            model_name="priceobservation",
            name="price",
            field=models.DecimalField(decimal_places=5, max_digits=12),
        ),
        migrations.RunPython(restore_procurement, remove_procurement),
    ]
