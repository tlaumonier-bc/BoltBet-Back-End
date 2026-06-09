from rest_framework import serializers
from .models import GridCell, LightningStrike

class GridCellSerializer(serializers.ModelSerializer):
    class Meta:
        model = GridCell
        fields = '__all__'

class LightningStrikeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LightningStrike
        fields = '__all__'