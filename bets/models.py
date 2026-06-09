from django.db import models
from django.conf import settings
from lightning.models import LightningStrike

class Bet(models.Model):
    STATUS = [
        ('pending', 'Pending'),
        ('won', 'Won'),
        ('lost', 'Lost'),
        ('cancelled', 'Cancelled')
    ]
    
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    cell_id = models.CharField(max_length=50)
    multiplier_at_bet = models.FloatField()
    amount = models.IntegerField()
    duration_minutes = models.IntegerField()
    placed_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    winning_strike = models.ForeignKey(LightningStrike, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20, choices=STATUS, default='pending')
    payout = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.user} - {self.amount} on {self.cell_id}"
