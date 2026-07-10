from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0004_player_country_code"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="grid_elo",
            field=models.IntegerField(default=1200),
        ),
        migrations.CreateModel(
            name="GridMatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("country", models.CharField(db_index=True, max_length=2)),
                ("status", models.CharField(choices=[("preparing", "preparing"), ("active", "active"), ("settled", "settled")], db_index=True, default="preparing", max_length=10)),
                ("bot_name", models.CharField(default="StormBot", max_length=40)),
                ("bot_elo", models.IntegerField(default=1200)),
                ("bot_elo_after", models.IntegerField(blank=True, null=True)),
                ("player_score", models.IntegerField(default=0)),
                ("bot_score", models.IntegerField(default=0)),
                ("grid_cols", models.IntegerField(default=8)),
                ("grid_rows", models.IntegerField(default=10)),
                ("strikes_30s_at_start", models.IntegerField(default=0)),
                ("elo_before", models.IntegerField(default=1200)),
                ("elo_after", models.IntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("prepare_ends_at", models.DateTimeField()),
                ("started_at", models.DateTimeField()),
                ("ends_at", models.DateTimeField()),
                ("settled_at", models.DateTimeField(blank=True, null=True)),
                ("player", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="grid_matches", to="account.player")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["player", "status"], name="account_gri_player__1dadb1_idx"),
                    models.Index(fields=["country", "-created_at"], name="account_gri_country_71f19d_idx"),
                ],
            },
        ),
    ]
