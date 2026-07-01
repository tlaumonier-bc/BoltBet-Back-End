from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0003_player_username_changed_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="country_code",
            field=models.CharField(blank=True, default="", max_length=2),
        ),
    ]
