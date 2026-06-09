from django.db import models

class LightningStrike(models.Model):
    external_id = models.CharField(max_length=100, unique=True)
    lat = models.FloatField()
    lon = models.FloatField()
    timestamp = models.DateTimeField(db_index=True)
    quality = models.CharField(max_length=20)  # good/medium/bad
    source = models.CharField(max_length=50, default='blitzortung')
    grid_cell = models.CharField(max_length=50, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['grid_cell', 'timestamp']),
        ]

    def __str__(self):
        return f"Strike at {self.lat}, {self.lon}"

class GridCell(models.Model):
    cell_id = models.CharField(max_length=50, primary_key=True)
    lon_min = models.IntegerField()
    lat_min = models.IntegerField()
    multiplier = models.FloatField(default=5.0)
    strike_count_24h = models.IntegerField(default=0)
    strike_count_1h = models.IntegerField(default=0)
    last_updated = models.DateTimeField(auto_now=True)
    
    # Future: real multiplier formula
    # multiplier = base_rate / (strike_count_1h + 0.5) * volatility_factor

    def __str__(self):
        return self.cell_id
