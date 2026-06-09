import asyncio
import json
import time
import uuid
import websockets
from django.core.management.base import BaseCommand
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

class Command(BaseCommand):
    help = 'Starts the WebSocket client to fetch real-time lightning data'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting live lightning stream worker..."))
        asyncio.run(self.listen_to_stream())

    async def listen_to_stream(self):
        # NOTE: Blitzortung's public websocket endpoint. 
        # The data here is often obfuscated. For this example, we assume decoded JSON.
        uri = "wss://ws1.blitzortung.org/" 
        channel_layer = get_channel_layer()

        async with websockets.connect(uri) as websocket:
            # Common initialization payload for their map websockets
            await websocket.send(json.dumps({"a": 111})) 
            self.stdout.write("Connected to external lightning stream.")

        while True:
                try:
                    message = await websocket.recv()
                    
                    # 1. PRINT THE RAW MESSAGE TYPE AND PREVIEW
                    # This tells you if it's bytes (binary) or str (text)
                    self.stdout.write(f"Received type: {type(message)}")
                    
                    # Print the first 100 characters to avoid flooding the terminal
                    # if it's a massive compressed blob
                    preview = str(message)[:100] 
                    self.stdout.write(f"Raw preview: {preview}")

                    # 2. ATTEMPT TO PARSE AS JSON (Wrapped in its own try/except)
                    try:
                        data = json.loads(message)
                        self.stdout.write(self.style.SUCCESS(f"Successfully parsed JSON: {json.dumps(data, indent=2)}"))
                    except json.JSONDecodeError:
                        self.stdout.write(self.style.WARNING("Message is not valid JSON. It requires custom decoding (e.g., LZW or binary unpacking)."))
                        
                    # --- MOCKING THE DECODED DATA FOR MVP ---
                    # Keep your mock data here for now so the frontend keeps working 
                    # while you reverse-engineer the real data format.
                    import random
                    mock_lat = (random.random() - 0.5) * 120
                    mock_lon = (random.random() - 0.5) * 360
                    
                    strike_data = {
                        "type": "strike",
                        "id": str(uuid.uuid4()),
                        "lat": mock_lat,
                        "lon": mock_lon,
                        "timestamp": int(time.time() * 1000),
                        "quality": "good"
                    }

                    await channel_layer.group_send(
                        'lightning_group',
                        {
                            'type': 'broadcast_message',
                            'message': strike_data
                        }
                    )
                    
                    await asyncio.sleep(0.5) 
                        
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Stream error: {e}"))
                    await asyncio.sleep(5)