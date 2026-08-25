import re

from django.db import migrations

PROPER_NAMES = {
    "arduino": "Arduino",
    "adafruit": "Adafruit",
    "bitalino": "BITalino",
    "digi-key": "Digi-Key",
    "digikey": "DigiKey",
    "elegoo": "Elegoo",
    "google": "Google",
    "grove": "Grove",
    "pololu": "Pololu",
    "raspi": "RasPi",
    "sharpie": "Sharpie",
    "sparkfun": "SparkFun",
}

PROPER_PHRASES = {
    "arduino mega": "Arduino Mega",
    "arduino nano": "Arduino Nano",
    "arduino pro micro": "Arduino Pro Micro",
    "arduino pro mini": "Arduino Pro Mini",
    "arduino uno": "Arduino Uno",
    "google cardboard": "Google Cardboard",
    "kee boar": "Kee Boar",
    "light blue bean": "Light Blue Bean",
    "mini-fit jr": "Mini-Fit Jr",
    "particle argon": "Particle Argon",
    "phys comp": "Phys Comp",
    "powerswitch tail ii": "PowerSwitch Tail II",
    "raspberry pi": "Raspberry Pi",
    "raspberry pi pico": "Raspberry Pi Pico",
    "vise grip": "Vise Grip",
}

WORD = re.compile(r"[^\W_]+(?:[-–][^\W_]+)*", re.UNICODE)


def sentence_case(value):
    first_word = True

    def normalize(match):
        nonlocal first_word
        word = match.group(0)
        is_first = first_word
        first_word = False

        proper = PROPER_NAMES.get(word.casefold())
        if proper:
            return proper

        letters = "".join(character for character in word if character.isalpha())
        segments = re.split(r"[-–]", word)
        has_internal_capital = any(
            any(character.isupper() for character in segment[1:])
            and any(character.islower() for character in segment)
            for segment in segments
        )
        if (
            any(character.isdigit() for character in word)
            or (letters and letters.isupper())
            or has_internal_capital
        ):
            return word

        normalized = word.lower()
        if is_first:
            normalized = normalized[:1].upper() + normalized[1:]
        return normalized

    normalized = WORD.sub(normalize, value)
    for phrase, spelling in PROPER_PHRASES.items():
        normalized = re.sub(
            rf"\b{re.escape(phrase)}\b",
            spelling,
            normalized,
            flags=re.IGNORECASE,
        )
    return normalized


def sentence_case_part_names(apps, schema_editor):
    Part = apps.get_model("inventory", "Part")
    changed = []

    for part in Part.objects.only("id", "short_name").iterator():
        normalized = sentence_case(part.short_name)
        if normalized != part.short_name:
            part.short_name = normalized
            changed.append(part)

    Part.objects.bulk_update(changed, ["short_name"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0009_restore_legacy_procurement")]

    operations = [
        migrations.RunPython(sentence_case_part_names, migrations.RunPython.noop)
    ]
