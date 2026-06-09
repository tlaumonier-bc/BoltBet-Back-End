import json
from channels.generic.websocket import AsyncWebsocketConsumer


class LightningConsumer(AsyncWebsocketConsumer):
    GROUP = "lightning_group"

    async def connect(self):
        await self.channel_layer.group_add(self.GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.GROUP, self.channel_name)

    # The method name MUST match the "type" used in group_send
    # (start_lightning_stream.py sends type="broadcast_message").
    async def broadcast_message(self, event):
        await self.send(text_data=json.dumps(event["message"]))