from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0002_alter_strikebet_outcome_alter_strikebet_scope_kind_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="username_changed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
