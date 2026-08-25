from django.db import migrations

LOCATION_MERGES = {
    "Deprecated": ("DEPRECATED", "deprecated"),
    "Fluids and pneumatics kit": (
        "Fluids and Pneumatics Kit",
        "Fluids and pneumatics kit",
    ),
    "Hardware drawer 3": (
        "Hardware Drawer 3",
        "Hardware drawer 3",
        "Hardware Drawers 3",
    ),
    "Hardware drawer 4": ("Hardware Drawer 4", "Hardware drawer 4"),
    "Hardware drawer 5": (
        "Hardware Drawer #5",
        "Hardware Drawer 5",
        "Hardware drawer #5",
        "Hardware drawer 5",
        "Hardware drawers 5",
    ),
    "Hardware drawer 7": (
        "Hardware Drawer #7",
        "Hardware Drawer 7",
        "Hardware drawer 7",
    ),
    "Lending": ("Lending", "lending"),
    "Rod rack": ("Rod rack", "rod rack"),
    "Soldering bench": (
        "Soldering Bench",
        "Soldering bench",
        "soldering bench",
    ),
    "Spool City": ("Spool City", "spool city"),
    "Tool box": ("Tool Box", "Tool box"),
}


def merge_duplicate_locations(apps, schema_editor):
    Location = apps.get_model("inventory", "Location")
    Part = apps.get_model("inventory", "Part")

    for canonical_code, duplicate_codes in LOCATION_MERGES.items():
        locations = list(Location.objects.filter(code__in=duplicate_codes))
        if not locations:
            continue

        locations.sort(
            key=lambda location: Part.objects.filter(location=location).count(),
            reverse=True,
        )
        canonical, *duplicates = locations

        names = [location.name for location in locations if location.name]
        notes = list(
            dict.fromkeys(location.notes for location in locations if location.notes)
        )

        for duplicate in duplicates:
            Part.objects.filter(location=duplicate).update(location=canonical)
            duplicate.delete()

        canonical.code = canonical_code
        canonical.name = canonical.name or (names[0] if names else "")
        canonical.notes = "\n\n".join(notes)
        canonical.is_active = any(location.is_active for location in locations)
        canonical.save(update_fields=["code", "name", "notes", "is_active"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0010_sentence_case_part_names")]

    operations = [
        migrations.RunPython(merge_duplicate_locations, migrations.RunPython.noop)
    ]
