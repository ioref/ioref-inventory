from django.db import migrations


def normalize_part_statuses(apps, schema_editor):
    Part = apps.get_model("inventory", "Part")
    Part.objects.filter(status="Discontinued").update(status="discontinued")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0011_merge_duplicate_locations")]

    operations = [
        migrations.RunPython(normalize_part_statuses, migrations.RunPython.noop)
    ]
