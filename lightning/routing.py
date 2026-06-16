from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/lightning/$", consumers.LightningConsumer.as_asgi()),
    re_path(r"public/ws/lightning/$", consumers.LightningConsumer.as_asgi()),
]