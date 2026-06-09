import asyncio
import json
import time
import uuid
import websockets
import ssl 
from django.core.management.base import BaseCommand
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

class Command(BaseCommand):
    help = 'Starts the WebSocket client to fetch real-time lightning data'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting live lightning stream worker..."))
        asyncio.run(self.listen_to_stream())

    def decode_lzw(self, data):
        """
        Blitzortung compresses their JSON strings using a custom LZW algorithm.
        This decodes the raw string back into readable JSON.
        """
        e = {}
        d = list(data)
        c = d[0]
        f = c
        g = [c]
        h = 256
        o = h
        for b in range(1, len(d)):
            a = ord(d[b])
            a = d[b] if h > a else e[a] if e.get(a) else f + c
            g.append(a)
            c = a[0]
            e[o] = f + c
            o += 1
            f = a
        return ''.join(g)

    async def listen_to_stream(self):
        # Using the standard ws1 endpoint without specific ports
        uri = "wss://ws1.blitzortung.org/" 
        channel_layer = get_channel_layer()

        # The Disguise
        headers = {
            "Origin": "https://map.blitzortung.org",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }

        # Bypass SSL locally
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        async with websockets.connect(uri, additional_headers=headers, ssl=ssl_context) as websocket:
            
            # The exact initialization payload they currently expect
            await websocket.send(json.dumps({"a": 111})) 
            self.stdout.write("Connected to external lightning stream! Waiting for strikes...")

            while True:
                try:
                    message = await websocket.recv()
 
                    try:
                        # 1. Attempt to decode the LZW compression
                        decoded_string = self.decode_lzw(message)
                        data = json.loads(decoded_string)
                        
                        # 2. Extract the real data
                        real_lat = data.get("lat")
                        real_lon = data.get("lon")
                        
                        # Convert nanoseconds to milliseconds
                        real_time_ms = int(data.get("time", 0) / 1_000_000) 
                        
                        # Determine quality based on number of detecting stations
                        stations_count = len(data.get("sig", []))
                        if stations_count >= 10:
                            quality = "good"
                        elif stations_count >= 5:
                            quality = "medium"
                        else:
                            quality = "bad"

                        # 3. Format exactly as your frontend/database expects
                        strike_data = {
                            "type": "strike",
                            "id": str(uuid.uuid4()),
                            "lat": real_lat,
                            "lon": real_lon,
                            "timestamp": real_time_ms,
                            "quality": quality
                        }

                        # Only broadcast if we actually got valid coordinates
                        if real_lat is not None and real_lon is not None:
                            self.stdout.write(self.style.SUCCESS(f"Broadcasting Strike! Lat: {real_lat}, Lon: {real_lon}"))
                            
                            await channel_layer.group_send(
                                'lightning_group',
                                {
                                    'type': 'broadcast_message',
                                    'message': strike_data
                                }
                            )
                        
                    except Exception as parse_error:
                        # Silently ignore unparseable chunks (usually keep-alive pings)
                        pass
                    
                except websockets.exceptions.ConnectionClosed as e:
                    self.stdout.write(self.style.ERROR(f"Connection closed by server: {e}"))
                    break # Trigger a reconnect
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Stream error: {e}"))
                    await asyncio.sleep(2)