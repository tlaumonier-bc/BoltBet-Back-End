"""
Live ingest worker. Connects to Blitzortung, decodes the feed, and:
  1. broadcasts each strike to the globe (unchanged behaviour), and
  2. buffers strikes and flushes them to Postgres every ~1s:
       - bulk INSERT into LightningStrike (dedup on external_id),
       - increment the per-minute StrikeRollupMinute counters (ON CONFLICT).

Run ONE instance (it is the writer for the rollup counters):

    python manage.py start_lightning_stream
"""

import asyncio
import datetime as dt
import json
import ssl
import time
import uuid

import websockets
from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand

FLUSH_INTERVAL_S = 1.0
MAX_BUFFER = 2000  # safety cap between flushes

# Increment-on-conflict upsert. Works on PostgreSQL and SQLite.
ROLLUP_UPSERT = """
INSERT INTO lightning_strikerollupminute
    (bucket, cell_id, count, good, medium, bad, latency_sum_ms, latency_n)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (bucket, cell_id) DO UPDATE SET
    count          = lightning_strikerollupminute.count          + excluded.count,
    good           = lightning_strikerollupminute.good           + excluded.good,
    medium         = lightning_strikerollupminute.medium         + excluded.medium,
    bad            = lightning_strikerollupminute.bad            + excluded.bad,
    latency_sum_ms = lightning_strikerollupminute.latency_sum_ms + excluded.latency_sum_ms,
    latency_n      = lightning_strikerollupminute.latency_n      + excluded.latency_n;
"""


class Command(BaseCommand):
    help = "Stream live lightning, broadcast it, and persist + roll it up."

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._buffer = []

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting live lightning stream worker..."))
        asyncio.run(self._run())

    # ---- Blitzortung custom LZW (unchanged) ----
    def decode_lzw(self, data):
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
        return "".join(g)

    async def _run(self):
        flusher = asyncio.create_task(self._flush_loop())
        try:
            await self._listen()
        finally:
            flusher.cancel()
            await self._flush()  # drain whatever is left

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            await self._flush()

    async def _listen(self):
        uri = "wss://ws1.blitzortung.org/"
        channel_layer = get_channel_layer()
        headers = {
            "Origin": "https://map.blitzortung.org",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        }
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        async with websockets.connect(uri, additional_headers=headers, ssl=ssl_context) as ws:
            await ws.send(json.dumps({"a": 111}))
            self.stdout.write("Connected to external lightning stream! Waiting for strikes...")

            while True:
                try:
                    message = await ws.recv()
                    try:
                        data = json.loads(self.decode_lzw(message))
                        lat, lon = data.get("lat"), data.get("lon")
                        if lat is None or lon is None:
                            continue

                        event_ms = int(data.get("time", 0) / 1_000_000)  # ns -> ms
                        received_ms = int(time.time() * 1000)
                        stations = len(data.get("sig", []))
                        quality = "good" if stations >= 10 else "medium" if stations >= 5 else "bad"

                        strike = {
                            "id": str(uuid.uuid4()),
                            "lat": lat,
                            "lon": lon,
                            "event_ms": event_ms,
                            "received_ms": received_ms,
                            "quality": quality,
                        }

                        # 1) live broadcast (frontend strike shape, unchanged)
                        await channel_layer.group_send(
                            "lightning_group",
                            {"type": "broadcast_message", "message": {
                                "type": "strike",
                                "id": strike["id"],
                                "lat": lat,
                                "lon": lon,
                                "timestamp": event_ms,
                                "quality": quality,
                            }},
                        )

                        # 2) buffer for persistence
                        self._buffer.append(strike)
                        if len(self._buffer) >= MAX_BUFFER:
                            await self._flush()

                    except Exception:
                        pass  # ignore keep-alives / unparseable frames

                except websockets.exceptions.ConnectionClosed as e:
                    self.stdout.write(self.style.ERROR(f"Connection closed by server: {e}"))
                    break
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Stream error: {e}"))
                    await asyncio.sleep(2)

    async def _flush(self):
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        try:
            await self._persist(batch)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"flush error ({len(batch)} strikes dropped): {e}"))

    @database_sync_to_async
    def _persist(self, batch):
        from django.db import connection
        from lightning.models import LightningStrike
        from lightning.grid import rollup_cell_for

        objs = []
        rollups = {}  # (bucket, cell_id) -> [count, good, medium, bad, lat_sum, lat_n]

        for s in batch:
            ts = dt.datetime.fromtimestamp(s["event_ms"] / 1000, tz=dt.timezone.utc)
            recv = dt.datetime.fromtimestamp(s["received_ms"] / 1000, tz=dt.timezone.utc)
            objs.append(LightningStrike(
                external_id=s["id"], lat=s["lat"], lon=s["lon"],
                timestamp=ts, received_at=recv, quality=s["quality"],
            ))

            cell_id, _, _ = rollup_cell_for(s["lat"], s["lon"])
            bucket = ts.replace(second=0, microsecond=0)
            latency_ms = max(0, min(120_000, s["received_ms"] - s["event_ms"]))
            r = rollups.setdefault((bucket, cell_id), [0, 0, 0, 0, 0, 0])
            r[0] += 1
            r[1 if s["quality"] == "good" else 2 if s["quality"] == "medium" else 3] += 1
            r[4] += latency_ms
            r[5] += 1

        LightningStrike.objects.bulk_create(objs, ignore_conflicts=True)

        if rollups:
            with connection.cursor() as cur:
                for (bucket, cell_id), (cnt, g, m, b, lsum, ln) in rollups.items():
                    cur.execute(ROLLUP_UPSERT, [bucket, cell_id, cnt, g, m, b, lsum, ln])

        self.stdout.write(f"flushed {len(objs)} strikes, {len(rollups)} rollup cells")
