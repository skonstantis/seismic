import asyncio
import websockets
from datetime import datetime, timedelta, timezone
from collections import deque
import json
import time
import signal
from aiohttp import web

BUFFER_250HZ_DURATION_SECONDS = 30
BUFFER_50HZ_DURATION_SECONDS = 300

buffer_250hz = deque()
buffer_50hz = deque()

buffer_250hz_lock = asyncio.Lock()
buffer_50hz_lock = asyncio.Lock()

connected_users = set()
connected_users_lock = asyncio.Lock()
broadcast_queue = asyncio.Queue()

virtual_time_base = None
last_gps_sync_monotonic = None

shutdown_event = asyncio.Event()

# === HTTP HANDLERS ===

async def handle_buffer_250hz(request):
    async with buffer_250hz_lock:
        buffer_copy = list(buffer_250hz)

    samples = [
        {"timestamp": ts, "value": value}
        for ts, value in buffer_copy
    ]
    return web.json_response({"samples": samples})

async def handle_buffer_50hz(request):
    async with buffer_50hz_lock:
        buffer_copy = list(buffer_50hz)

    samples = [
        {"timestamp": ts, "value": value}
        for ts, value in buffer_copy
    ]
    return web.json_response({"samples": samples})

# === SAFE SEND ===

async def safe_send(ws, message):
    try:
        await asyncio.wait_for(ws.send(message), timeout=0.1)
    except Exception as e:
        print(f"Client send failed: {ws.remote_address} ({e})", flush=True)
        async with connected_users_lock:
            connected_users.discard(ws)
        try:
            await ws.close(code=1011, reason="Too slow to keep up")
        except Exception as close_err:
            print(f"Error closing socket for {ws.remote_address}: {close_err}", flush=True)

# === VIRTUAL CLOCK LOOP ===

async def virtual_clock_loop():
    global virtual_time_base, last_gps_sync_monotonic

    while not shutdown_event.is_set():
        await asyncio.sleep(1)

        if virtual_time_base and last_gps_sync_monotonic is not None:
            virtual_time_now = virtual_time_base + timedelta(seconds=(time.monotonic() - last_gps_sync_monotonic))

            cutoff_ts_250hz = (virtual_time_now - timedelta(seconds=BUFFER_250HZ_DURATION_SECONDS)).timestamp()
            cutoff_ts_50hz = (virtual_time_now - timedelta(seconds=BUFFER_50HZ_DURATION_SECONDS)).timestamp()

            async with buffer_250hz_lock:
                while buffer_250hz and buffer_250hz[0][0] < cutoff_ts_250hz:
                    buffer_250hz.popleft()

            async with buffer_50hz_lock:
                while buffer_50hz and buffer_50hz[0][0] < cutoff_ts_50hz:
                    buffer_50hz.popleft()

            if buffer_250hz:
                print(f"[BUFFER 250Hz] {len(buffer_250hz)} samples | "
                      f"{datetime.utcfromtimestamp(buffer_250hz[0][0])} - "
                      f"{datetime.utcfromtimestamp(buffer_250hz[-1][0])}", flush=True)
            else:
                print("[BUFFER 250Hz] Empty", flush=True)

            if buffer_50hz:
                print(f"[BUFFER  50Hz] {len(buffer_50hz)} samples | "
                      f"{datetime.utcfromtimestamp(buffer_50hz[0][0])} - "
                      f"{datetime.utcfromtimestamp(buffer_50hz[-1][0])}", flush=True)
            else:
                print("[BUFFER  50Hz] Empty", flush=True)

# === BROADCASTER ===

async def broadcaster():
    global virtual_time_base, last_gps_sync_monotonic

    while not shutdown_event.is_set():
        raw_message = await broadcast_queue.get()

        try:
            packet = json.loads(raw_message)
            timestamp_start = datetime.fromisoformat(packet["timestamp_start"].replace("Z", "+00:00")).astimezone(timezone.utc)
            sample_rate = packet["sample_rate"]
            samples = packet["samples"]
            gps_synced = packet.get("gps_synced", False)
        except Exception as e:
            print(f"[ERROR] Invalid station packet: {e}", flush=True)
            continue

        if gps_synced and sample_rate > 0 and len(samples) > 0:
            duration = len(samples) / sample_rate
            new_virtual_time = timestamp_start + timedelta(seconds=duration)

            if virtual_time_base and new_virtual_time < virtual_time_base:
                print(f"[INFO] GPS time moved backward: {virtual_time_base} → {new_virtual_time}", flush=True)

            virtual_time_base = new_virtual_time
            last_gps_sync_monotonic = time.monotonic()
            print(f"[GPS SYNC] virtual_time_base={virtual_time_base}, monotonic={last_gps_sync_monotonic}", flush=True)

        if not (virtual_time_base and last_gps_sync_monotonic is not None):
            print("[WARNING] No GPS sync yet; dropping data", flush=True)
            continue

        new_samples_250hz = []
        downsampled_50hz = []

        for i, value in enumerate(samples):
            ts = timestamp_start + timedelta(seconds=i / sample_rate)
            ts_float = ts.timestamp()
            new_samples_250hz.append((ts_float, value))

            if i == 0 or i == len(samples) - 1 or i % 5 == 0:
                downsampled_50hz.append((ts_float, value))

        async with buffer_250hz_lock:
            buffer_250hz.extend(new_samples_250hz)

        async with buffer_50hz_lock:
            buffer_50hz.extend(downsampled_50hz)

        async with connected_users_lock:
            users_copy = list(connected_users)

        packet_to_send = packet.copy()
        packet_to_send["samples"] = [
            {"timestamp": ts, "value": value}
            for ts, value in new_samples_250hz
        ]

        message_to_send = json.dumps(packet_to_send)

        coros = [safe_send(ws, message_to_send) for ws in users_copy]
        results = await asyncio.gather(*coros, return_exceptions=True)

        for ws, result in zip(users_copy, results):
            if isinstance(result, Exception):
                print(f"[ERROR] Broadcast to {ws.remote_address} failed: {result}", flush=True)

# === WEBSOCKET HANDLERS ===

async def station_handler(websocket):
    print(f"New station connection from {websocket.remote_address}", flush=True)

    async def watchdog():
        try:
            await websocket.wait_closed()
        finally:
            print(f"Station connection closed (finally) {websocket.remote_address}", flush=True)

    watchdog_task = asyncio.create_task(watchdog())

    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=3.0)
                await websocket.send("Echo station: OK")
                await broadcast_queue.put(message)
            except asyncio.TimeoutError:
                print(f"Inactivity timeout. Closing connection {websocket.remote_address}", flush=True)
                await websocket.close(code=1000, reason="Inactivity timeout")
                break
    except websockets.exceptions.ConnectionClosedOK:
        print("Station client disconnected cleanly", flush=True)
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"Station client disconnected with error: {e}", flush=True)
    except Exception as e:
        print(f"Unexpected error in station handler: {e}", flush=True)
    finally:
        watchdog_task.cancel()

async def user_handler(websocket):
    print(f"New user connection from {websocket.remote_address}", flush=True)

    async with connected_users_lock:
        connected_users.add(websocket)

    async def watchdog():
        try:
            await websocket.wait_closed()
        finally:
            print(f"User connection closed (finally) {websocket.remote_address}", flush=True)
            async with connected_users_lock:
                connected_users.discard(websocket)

    watchdog_task = asyncio.create_task(watchdog())

    try:
        async for message in websocket:
            await websocket.send(f"Echo user: {message}")
    except websockets.exceptions.ConnectionClosedOK:
        print("User client disconnected cleanly", flush=True)
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"User client disconnected with error: {e}", flush=True)
    except Exception as e:
        print(f"Unexpected error in user handler: {e}", flush=True)
    finally:
        watchdog_task.cancel()
        async with connected_users_lock:
            connected_users.discard(websocket)

# === MAIN ===

def handle_shutdown_signal():
    print("Shutting down...", flush=True)
    shutdown_event.set()

async def main():
    signal.signal(signal.SIGINT, lambda s, f: handle_shutdown_signal())
    signal.signal(signal.SIGTERM, lambda s, f: handle_shutdown_signal())

    station_server = websockets.serve(station_handler, "127.0.0.1", 8765, ping_interval=None)
    user_server = websockets.serve(user_handler, "127.0.0.1", 8766, ping_interval=20, ping_timeout=10)

    app = web.Application()
    app.router.add_get("/buffer30", handle_buffer_250hz)
    app.router.add_get("/buffer300", handle_buffer_50hz)

    runner = web.AppRunner(app)
    await runner.setup()
    http_site = web.TCPSite(runner, "127.0.0.1", 8080)
    await http_site.start()

    async with station_server, user_server:
        print("WebSocket servers running on ports 8765 (station) and 8766 (users)", flush=True)

        broadcaster_task = asyncio.create_task(broadcaster())
        clock_task = asyncio.create_task(virtual_clock_loop())

        await shutdown_event.wait()

        print("Shutting down HTTP server...", flush=True)
        await runner.cleanup()

        print("Cancelling tasks...", flush=True)
        broadcaster_task.cancel()
        clock_task.cancel()
        await asyncio.gather(broadcaster_task, clock_task, return_exceptions=True)
        print("Shutdown complete.", flush=True)

if __name__ == "__main__":
    asyncio.run(main())