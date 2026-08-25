import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0012_normalize_part_statuses")]

    operations = [
        migrations.AlterField(
            model_name="priceobservation",
            name="observed_at",
            field=models.DateTimeField(
                default=django.utils.timezone.now,
                help_text="When the price was observed.",
            ),
        )
    ]
