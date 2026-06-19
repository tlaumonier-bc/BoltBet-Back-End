from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Player',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('username', models.CharField(max_length=20)),
                ('username_lower', models.CharField(max_length=20, unique=True)),
                ('tokens', models.IntegerField(default=100)),
                ('provider', models.CharField(blank=True, default='', max_length=20)),
                ('provider_subject', models.CharField(blank=True, default='', max_length=255)),
                ('wins', models.IntegerField(default=0)),
                ('games_played', models.IntegerField(default=0)),
                ('retired', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name='player',
            constraint=models.UniqueConstraint(
                condition=models.Q(('provider__gt', '')),
                fields=('provider', 'provider_subject'),
                name='uniq_oauth_identity',
            ),
        ),
        migrations.CreateModel(
            name='Session',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.CharField(db_index=True, max_length=64, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sessions', to='account.player')),
            ],
        ),
        migrations.CreateModel(
            name='StrikeBet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('round_id', models.BigIntegerField(db_index=True)),
                ('side', models.CharField(max_length=4)),
                ('amount', models.IntegerField()),
                ('scope_kind', models.CharField(max_length=8)),
                ('scope_id', models.CharField(max_length=8)),
                ('prev_count', models.IntegerField()),
                ('final_count', models.IntegerField(blank=True, null=True)),
                ('outcome', models.CharField(blank=True, max_length=5, null=True)),
                ('payout', models.IntegerField(blank=True, null=True)),
                ('status', models.CharField(db_index=True, default='pending', max_length=8)),
                ('placed_at', models.DateTimeField(auto_now_add=True)),
                ('settled_at', models.DateTimeField(blank=True, null=True)),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bets', to='account.player')),
            ],
        ),
        migrations.AddIndex(
            model_name='strikebet',
            index=models.Index(fields=['status', 'round_id'], name='account_str_status_idx'),
        ),
        migrations.AddConstraint(
            model_name='strikebet',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status', 'pending')),
                fields=('player',),
                name='uniq_pending_bet_per_player',
            ),
        ),
    ]