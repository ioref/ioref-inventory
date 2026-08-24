from django.db import migrations


GROUPS_BY_CATEGORY = {
    "input": (
        "accelerometers",
        "distance-sensors",
        "encoders",
        "infrared-receivers",
        "joysticks",
        "lever-switches",
        "photoresistors",
        "potentiometers",
        "proximity-sensors",
        "pushbuttons",
        "slide-switches",
        "tactile-pushbuttons",
        "temperature-and-humidity-sensors",
        "thermistors",
        "tilt-switches",
        "ultrasonic-rangefinders",
    ),
    "output": (
        "active-buzzers",
        "dc-motors",
        "h-bridge-motor-drivers",
        "infrared-remotes",
        "lcd-displays",
        "leds",
        "passive-buzzers",
        "servo-motors",
        "shift-registers",
        "stepper-motors",
        "vibration-motors",
    ),
    "power": (
        "batteries",
        "capacitors",
        "diodes",
        "power-supplies",
        "relays",
        "resistor",
        "stepper-motor-drivers",
        "transistor",
    ),
    "connector": ("breadboard", "wire"),
    "controller": ("microcontroller-boards",),
}

UNGROUPED_PARTS_BY_CATEGORY = {
    "input": ("0286", "0574"),
}


def seed_assignments(apps, schema_editor):
    Category = apps.get_model("inventory", "Category")
    Group = apps.get_model("inventory", "Group")
    Part = apps.get_model("inventory", "Part")

    categories = {category.slug: category for category in Category.objects.all()}
    for category_slug, group_slugs in GROUPS_BY_CATEGORY.items():
        Group.objects.filter(slug__in=group_slugs).update(
            category=categories[category_slug]
        )

    for category_slug, part_numbers in UNGROUPED_PARTS_BY_CATEGORY.items():
        Part.objects.filter(
            part_number__in=part_numbers, group__isnull=True
        ).update(category=categories[category_slug])


class Migration(migrations.Migration):
    dependencies = [
        (
            "inventory",
            "0007_part_category_part_part_category_only_without_group",
        ),
    ]

    operations = [
        migrations.RunPython(seed_assignments, migrations.RunPython.noop),
    ]
