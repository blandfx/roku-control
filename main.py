import asyncio
import datetime
import os
import time
import uuid
import xml.etree.ElementTree as ET
import aiohttp
import pyytlounge
import pyytlounge.models
import pyytlounge.wrapper
pyytlounge.models.BLACKLISTED_CLIENTS = []
pyytlounge.wrapper.BLACKLISTED_CLIENTS = []
import typing
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel


from database import (
    db_init,
    db_get_devices,
    db_add_device,
    db_remove_device,
    db_update_device_screen_id,
    db_add_history_entry,
    db_delete_history_entry,
    db_get_history,
    db_get_last_played_for_device,
    db_get_setting,
    db_set_setting
)
from plex_support import (
    PlexError,
    get_account as plex_get_account,
    get_art as plex_get_art,
    get_kid_catalog as plex_get_kid_catalog,
    get_media_state as get_plex_media_state,
    invalidate_resource_cache,
    is_plex_video_id,
    poll_pin_auth,
    resolve_catalog_media as resolve_plex_catalog_media,
    resume_media as resume_plex_media,
    start_pin_auth,
)

app = FastAPI(title="Roku Control")

# Global dict to store in-memory status and active remote sessions for each Roku
active_sessions = {}
# Dict to hold background task references
device_tasks = {}
# Video details cache (video_id -> (title, thumbnail_url))
video_details_cache = {}
pending_plex_pins = {}
FAST_MONITOR_SECONDS = 30.0


def request_fast_monitoring(ip: str, duration: float = FAST_MONITOR_SECONDS):
    """Wake a device monitor and keep it polling rapidly after a playback command."""
    session_info = active_sessions.get(ip)
    if not session_info:
        return
    loop = asyncio.get_running_loop()
    session_info["fast_poll_until"] = max(
        session_info.get("fast_poll_until", 0.0),
        loop.time() + duration,
    )
    session_info["monitor_wake"].set()


async def wait_for_next_monitor_poll(session_info, normal_delay: float = 10.0):
    loop = asyncio.get_running_loop()
    delay = 1.0 if loop.time() < session_info.get("fast_poll_until", 0.0) else normal_delay
    wake = session_info["monitor_wake"]
    try:
        await asyncio.wait_for(wake.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
    finally:
        wake.clear()


async def refresh_plex_catalog_background(token: str):
    try:
        result = await plex_get_kid_catalog(get_plex_client_id(), token, force=True)
        print(f'Loaded {len(result["items"])} Plex titles from Kid libraries.')
    except Exception as exc:
        print(f"Could not refresh Plex Kid catalog: {exc}")


def get_plex_client_id():
    client_id = db_get_setting("plex_client_id")
    if not client_id:
        client_id = uuid.uuid4().hex
        db_set_setting("plex_client_id", client_id)
    return client_id

class DeviceAddSchema(BaseModel):
    ip: str
    name: str

class ControlSchema(BaseModel):
    ip: str
    command: str
    value: typing.Optional[typing.Any] = None

class PlaySchema(BaseModel):
    ip: str
    video_id: str
    position: float = 0.0


class PlexCatalogPlaySchema(BaseModel):
    ip: str
    video_id: str

# Helpers for ECP Queries
async def get_active_app(ip: str):
    try:
        url = f"http://{ip}:8060/query/active-app"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=3) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    root = ET.fromstring(text)
                    app_node = root.find("app")
                    if app_node is not None:
                        app_id = app_node.get("id")
                        app_name = app_node.text
                        return app_id, app_name
    except Exception:
        pass
    return None, None

async def check_is_roku(ip: str):
    try:
        url = f"http://{ip}:8060/query/device-info"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=3) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if "<device-info>" in text:
                        return True
    except Exception:
        pass
    return False

async def get_youtube_screen_id(ip: str):
    try:
        url = f"http://{ip}:8060/dial/YouTube"
        headers = {"Origin": "https://www.youtube.com"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=3) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    root = ET.fromstring(text)
                    # Support namespaces dynamically
                    for node in root.iter():
                        if node.tag.endswith("screenId"):
                            return node.text
    except Exception as e:
        print(f"Error getting screenId for {ip}: {e}")
    return None

async def get_roku_media_player_state(ip: str):
    try:
        url = f"http://{ip}:8060/query/media-player"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=3) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    root = ET.fromstring(text)

                    state = root.get("state")
                    error = root.get("error")

                    plugin_node = root.find("plugin")
                    plugin_name = plugin_node.get("name") if plugin_node is not None else None
                    plugin_id = plugin_node.get("id") if plugin_node is not None else None

                    format_node = root.find("format")
                    video_res = format_node.get("video_res") if format_node is not None else None
                    container = format_node.get("container") if format_node is not None else None

                    position_node = root.find("position")
                    pos_text = position_node.text if position_node is not None else None
                    position_ms = 0
                    if pos_text and "ms" in pos_text:
                        position_ms = int(pos_text.split()[0])

                    duration_node = root.find("duration")
                    dur_text = duration_node.text if duration_node is not None else None
                    duration_ms = 0
                    if dur_text and "ms" in dur_text:
                        duration_ms = int(dur_text.split()[0])

                    return {
                        "state": state,
                        "error": error,
                        "plugin_name": plugin_name,
                        "plugin_id": plugin_id,
                        "video_res": video_res,
                        "container": container,
                        "position_ms": position_ms,
                        "duration_ms": duration_ms
                    }
    except Exception as e:
        print(f"Error querying media-player for {ip}: {e}")
    return None

async def fetch_video_details(video_id: str):
    try:
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=4) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("title"), data.get("thumbnail_url")
    except Exception:
        pass
    return f"YouTube Video ({video_id})", f"https://img.youtube.com/vi/{video_id}/0.jpg"


# Event Handler for Lounge Stream Updates
async def handle_device_event(device_ip: str, device_name: str, event):
    if device_ip not in active_sessions:
        return

    session_info = active_sessions[device_ip]
    current_playing = session_info["playing_state"] or {}

    # Extract details
    video_id = getattr(event, "video_id", current_playing.get("video_id"))
    current_time = getattr(event, "current_time", current_playing.get("current_time", 0.0))
    duration = getattr(event, "duration", current_playing.get("duration", 0.0))

    # State string
    raw_state = getattr(event, "state", None)
    state_str = raw_state.name if raw_state else current_playing.get("state", "Stopped")

    if video_id:
        title, thumbnail_url = video_details_cache.get(video_id, (None, None))
        if not title:
            title, thumbnail_url = await fetch_video_details(video_id)
            video_details_cache[video_id] = (title, thumbnail_url)

        session_info["playing_state"] = {
            "video_id": video_id,
            "title": title,
            "thumbnail_url": thumbnail_url,
            "current_time": current_time,
            "duration": duration,
            "state": state_str,
            "last_event_time": datetime.datetime.now().isoformat()
        }
        session_info["status"] = "playing" if state_str == "Playing" else "idle"

        # Log to playback history (only if it is actively playing or paused, not stopped)
        if state_str in ("Playing", "Paused"):
            db_add_history_entry(
                device_ip=device_ip,
                device_name=device_name,
                video_id=video_id,
                video_title=title,
                thumbnail_url=thumbnail_url,
                duration=duration,
                position=current_time
            )
    else:
        session_info["playing_state"] = None
        session_info["status"] = "idle"


# Background Device Monitor Task Loop
async def monitor_device(ip: str, name: str):
    print(f"Starting monitor task for {name} ({ip})")
    active_sessions[ip] = {
        "ip": ip,
        "name": name,
        "status": "offline",
        "playing_state": None,
        "api": None,
        "subscribe_task": None,
        "next_connect_time": 0.0,
        "resuming": False,
        "plex_command_id": 1,
        "fast_poll_until": 0.0,
        "monitor_wake": asyncio.Event(),
    }

    class DeviceEventListener(pyytlounge.EventListener):
        def __init__(self, device_ip, device_name):
            self.device_ip = device_ip
            self.device_name = device_name

        async def now_playing_changed(self, event):
            await handle_device_event(self.device_ip, self.device_name, event)

        async def playback_state_changed(self, event):
            await handle_device_event(self.device_ip, self.device_name, event)

        async def disconnected(self, event):
            print(f"Lounge connection disconnected for {self.device_name} ({self.device_ip})")
            if self.device_ip in active_sessions:
                active_sessions[self.device_ip]["status"] = "idle"
                active_sessions[self.device_ip]["playing_state"] = None

    listener = DeviceEventListener(ip, name)

    while True:
        try:
            session_info = active_sessions.get(ip)
            if session_info and session_info.get("resuming"):
                await wait_for_next_monitor_poll(session_info, normal_delay=0.5)
                continue

            app_id, app_name = await get_active_app(ip)
            if app_id == "837" or app_name == "YouTube":
                session_info = active_sessions[ip]
                api = session_info["api"]

                if api is None or not api.connected():
                    # Cooldown check
                    now = asyncio.get_event_loop().time()
                    if now < session_info.get("next_connect_time", 0.0):
                        session_info["status"] = "idle"
                    else:
                        # Clean up old connections
                        if session_info["subscribe_task"]:
                            session_info["subscribe_task"].cancel()
                            session_info["subscribe_task"] = None
                        if api:
                            try:
                                await api.close()
                            except Exception:
                                pass
                            session_info["api"] = None

                        # Query screen_id
                        screen_id = await get_youtube_screen_id(ip)
                        if screen_id:
                            db_update_device_screen_id(ip, screen_id)

                            api = pyytlounge.YtLoungeApi("AntigravityRemote", listener)
                            session_info["api"] = api
                            await api.__aenter__()

                            print(f"Pairing {name} with screenId {screen_id}...")
                            paired = await api.pair_with_screen_id(screen_id)
                            if paired:
                                print(f"Connecting to YouTube Lounge on {name}...")
                                connected = await api.connect()
                                if connected:
                                    print(f"Connected successfully to {name}!")
                                    session_info["subscribe_task"] = asyncio.create_task(api.subscribe())
                                    session_info["status"] = "idle"
                                    # Reset cooldown on success
                                    session_info["next_connect_time"] = 0.0
                                    await asyncio.sleep(2.0)
                                    await api.get_now_playing()
                                else:
                                    print(f"Lounge connect failed for {name}")
                                    await api.__aexit__(None, None, None)
                                    session_info["api"] = None
                                    session_info["status"] = "idle"
                                    session_info["next_connect_time"] = now + 120.0 # 2 mins backoff
                            else:
                                print(f"Lounge pairing failed for {name}")
                                await api.__aexit__(None, None, None)
                                session_info["api"] = None
                                session_info["status"] = "idle"
                                session_info["next_connect_time"] = now + 120.0 # 2 mins backoff
                        else:
                            session_info["status"] = "idle"
                else:
                    if session_info["status"] == "offline":
                        session_info["status"] = "idle"
            elif app_id == "13535" or (app_name and "Plex" in app_name):
                session_info = active_sessions[ip]
                # Cleanup YouTube lounge connections if active
                if session_info["subscribe_task"]:
                    session_info["subscribe_task"].cancel()
                    session_info["subscribe_task"] = None
                if session_info["api"]:
                    try:
                        await session_info["api"].__aexit__(None, None, None)
                    except Exception:
                        pass
                    session_info["api"] = None

                # Plex exposes stable server and library identifiers through its local
                # Companion timeline. The connected account resolves shared/remote servers.
                plex_token = db_get_setting("plex_token")
                try:
                    media_state = await get_plex_media_state(
                        ip,
                        get_plex_client_id(),
                        plex_token,
                        session_info.get("plex_command_id", 1),
                    )
                    session_info["plex_command_id"] = session_info.get("plex_command_id", 1) + 1
                except PlexError as exc:
                    print(f"Plex metadata unavailable for {name}: {exc}")
                    media_state = None

                # Keep basic position monitoring available before Plex is connected.
                if not media_state:
                    media_state = await get_roku_media_player_state(ip)

                if media_state and media_state.get("state") in ("play", "pause"):
                    state_str = "Playing" if media_state["state"] == "play" else "Paused"
                    position = media_state["position_ms"] / 1000.0
                    duration = media_state["duration_ms"] / 1000.0

                    # Format duration into human readable string (e.g. 1h 45m or 22m 15s)
                    dur_ms = media_state.get("duration_ms", 0)
                    seconds = int(dur_ms / 1000)
                    minutes = int(seconds / 60)
                    hours = int(minutes / 60)

                    if hours > 0:
                        dur_str = f"{hours}h {minutes % 60}m"
                    elif minutes > 0:
                        dur_str = f"{minutes}m {seconds % 60}s"
                    else:
                        dur_str = f"{seconds}s"

                    res_str = f" @ {media_state['video_res']}" if media_state.get('video_res') else ""
                    fmt_str = f" ({media_state['container']})" if media_state.get('container') else ""

                    default_title = f"Plex: {dur_str}{res_str}{fmt_str}"

                    video_id = media_state.get("video_id") or f"plex_{dur_ms}"
                    title = media_state.get("title") or default_title
                    thumbnail_url = media_state.get("thumbnail_url") or "https://images.unsplash.com/photo-1594909122845-11baa439b7bf?w=400&q=80"

                    session_info["playing_state"] = {
                        "video_id": video_id,
                        "title": title,
                        "thumbnail_url": thumbnail_url,
                        "current_time": position,
                        "duration": duration,
                        "state": state_str,
                        "last_event_time": datetime.datetime.now().isoformat()
                    }
                    session_info["status"] = "playing" if state_str == "Playing" else "idle"

                    # Log to history
                    db_add_history_entry(
                        device_ip=ip,
                        device_name=name,
                        video_id=video_id,
                        video_title=title,
                        thumbnail_url=thumbnail_url,
                        duration=duration,
                        position=position
                    )
                else:
                    session_info["playing_state"] = None
                    session_info["status"] = "idle"
            else:
                session_info = active_sessions[ip]
                session_info["status"] = "offline" if app_id is None else "idle"
                session_info["playing_state"] = None

                if session_info["subscribe_task"]:
                    session_info["subscribe_task"].cancel()
                    session_info["subscribe_task"] = None
                if session_info["api"]:
                    try:
                        await session_info["api"].__aexit__(None, None, None)
                    except Exception:
                        pass
                    session_info["api"] = None

        except asyncio.CancelledError:
            print(f"Monitor task for {name} ({ip}) cancelled.")
            session_info = active_sessions.get(ip)
            if session_info:
                if session_info["subscribe_task"]:
                    session_info["subscribe_task"].cancel()
                if session_info["api"]:
                    try:
                        await session_info["api"].__aexit__(None, None, None)
                    except Exception:
                        pass
            break
        except Exception as e:
            print(f"Error in monitor loop for {name} ({ip}): {e}")
            if ip in active_sessions:
                active_sessions[ip]["status"] = "idle"
                now = asyncio.get_event_loop().time()
                active_sessions[ip]["next_connect_time"] = now + 120.0 # 2 mins backoff

                # Cleanup connection objects
                if active_sessions[ip]["subscribe_task"]:
                    active_sessions[ip]["subscribe_task"].cancel()
                    active_sessions[ip]["subscribe_task"] = None
                if active_sessions[ip]["api"]:
                    try:
                        await active_sessions[ip]["api"].__aexit__(None, None, None)
                    except Exception:
                        pass
                    active_sessions[ip]["api"] = None

        session_info = active_sessions.get(ip)
        if session_info:
            await wait_for_next_monitor_poll(session_info)
        else:
            await asyncio.sleep(10)


# Resume Video Action Task
async def resume_playback_background(ip: str, video_id: str, position: float):
    session_info = active_sessions.get(ip)
    if session_info:
        session_info["resuming"] = True
        request_fast_monitoring(ip)
    try:
        if is_plex_video_id(video_id):
            result = await resume_plex_media(
                ip,
                get_plex_client_id(),
                db_get_setting("plex_token"),
                video_id,
                position,
            )
            print(f'Plex resumed "{result["title"]}" on {ip}!')
            return

        # 1. Launch YouTube via ECP
        url = f"http://{ip}:8060/launch/837?contentId={video_id}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, timeout=5) as resp:
                resp.raise_for_status()

        # 2. Wait and poll for YouTube to load and expose a screenId
        screen_id = None
        for attempt in range(25):
            screen_id = await get_youtube_screen_id(ip)
            if screen_id:
                break
            print(f"Attempt {attempt+1}: screenId not ready yet on {ip}, waiting...")
            await asyncio.sleep(1.5)

        if not screen_id:
            print(f"Failed to get screenId for resume casting on {ip} after launch")
            return

        # 4. Terminate any active main monitoring connection temporarily
        if session_info:
            if session_info["subscribe_task"]:
                session_info["subscribe_task"].cancel()
                session_info["subscribe_task"] = None
            if session_info["api"]:
                try:
                    await session_info["api"].__aexit__(None, None, None)
                except Exception:
                    pass
                session_info["api"] = None

            # Create a temporary Remote controller
            class TempListener(pyytlounge.EventListener):
                pass

            api = pyytlounge.YtLoungeApi("ResumeRemote", TempListener())
            session_info["api"] = api
            await api.__aenter__()
            paired = await api.pair_with_screen_id(screen_id)
            if paired:
                connected = await api.connect()
                if connected:
                    # Request to play and seek
                    await api.play_video(video_id)
                    await asyncio.sleep(2.5)
                    if position > 0:
                        await api.seek_to(position)

                    # Close remote session implicitly to keep playing
                    await api.__aexit__(None, None, None)
                    print(f"Playback resumed successfully on {ip}!")
    except Exception as e:
        print(f"Error resuming playback in background: {e}")
    finally:
        if session_info:
            session_info["resuming"] = False
            session_info["api"] = None
            request_fast_monitoring(ip)


# FastAPI Lifecycle Setup
@app.on_event("startup")
async def startup_event():
    db_init()
    devices = db_get_devices()
    for d in devices:
        ip = d["ip"]
        name = d["name"]
        device_tasks[ip] = asyncio.create_task(monitor_device(ip, name))

@app.on_event("shutdown")
async def shutdown_event():
    for ip, task in list(device_tasks.items()):
        task.cancel()
    await asyncio.sleep(1)


# API Web Endpoints
@app.get("/", response_class=HTMLResponse)
async def get_index():
    try:
        with open(os.path.join(os.path.dirname(__file__), "templates", "index.html"), "r") as f:
            return f.read()
    except Exception as e:
        return f"<h1>Error loading index.html: {e}</h1>"

@app.get("/api/devices")
async def api_devices():
    # Merge DB config with live active_sessions statuses
    devices = db_get_devices()
    result = []
    now = asyncio.get_event_loop().time()
    for d in devices:
        ip = d["ip"]
        live = active_sessions.get(ip, {})
        next_connect = live.get("next_connect_time", 0.0)
        cooldown_remaining = max(0.0, next_connect - now)
        result.append({
            "ip": ip,
            "name": d["name"],
            "screen_id": d["screen_id"],
            "status": live.get("status", "offline"),
            "playing_state": live.get("playing_state"),
            "cooldown_remaining": round(cooldown_remaining)
        })
    return result

@app.post("/api/devices")
async def api_add_device(device: DeviceAddSchema):
    # Validate Roku device via ECP first
    is_roku = await check_is_roku(device.ip)
    if not is_roku:
        raise HTTPException(status_code=400, detail="Provided IP address does not appear to be a responsive Roku device.")

    db_add_device(device.ip, device.name)

    # Start monitor task
    if device.ip in device_tasks:
        device_tasks[device.ip].cancel()
    device_tasks[device.ip] = asyncio.create_task(monitor_device(device.ip, device.name))

    return {"success": True}

@app.delete("/api/devices/{ip}")
async def api_delete_device(ip: str):
    db_remove_device(ip)

    if ip in device_tasks:
        device_tasks[ip].cancel()
        del device_tasks[ip]

    if ip in active_sessions:
        del active_sessions[ip]

    return {"success": True}

@app.get("/api/history")
async def api_history():
    return db_get_history(limit=50)

@app.delete("/api/history/{history_id}")
async def api_delete_history(history_id: int):
    deleted = db_delete_history_entry(history_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="History item not found.")
    return {"success": True, "deleted": deleted}

@app.post("/api/play")
async def api_play(payload: PlaySchema):
    if is_plex_video_id(payload.video_id):
        session_info = active_sessions.get(payload.ip)
        if session_info:
            session_info["resuming"] = True
            request_fast_monitoring(payload.ip)
        try:
            result = await resume_plex_media(
                payload.ip,
                get_plex_client_id(),
                db_get_setting("plex_token"),
                payload.video_id,
                payload.position,
            )
            return {"success": True, "method": "Plex Companion", **result}
        except PlexError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            if session_info:
                session_info["resuming"] = False
                request_fast_monitoring(payload.ip)
    asyncio.create_task(resume_playback_background(payload.ip, payload.video_id, payload.position))
    return {"success": True}

@app.post("/api/control")
async def api_control(payload: ControlSchema):
    ip = payload.ip
    command = payload.command
    value = payload.value

    if ip not in active_sessions:
        raise HTTPException(status_code=404, detail="Device not found/monitored")

    session_info = active_sessions[ip]
    api = session_info["api"]

    if command == "power_off":
        try:
            async with aiohttp.ClientSession() as session:
                for ecp_cmd in ("Home", "PowerOff"):
                    async with session.post(f"http://{ip}:8060/keypress/{ecp_cmd}", timeout=3) as response:
                        if response.status not in (200, 202):
                            raise RuntimeError(f"{ecp_cmd} returned HTTP {response.status}")
                    if ecp_cmd == "Home":
                        await asyncio.sleep(0.5)
            playback_stopped = False
            for _ in range(12):
                app_id, app_name = await get_active_app(ip)
                if not (app_id in ("837", "13535") or app_name == "YouTube" or (app_name and "Plex" in app_name)):
                    playback_stopped = True
                    session_info["playing_state"] = None
                    session_info["status"] = "offline" if app_id is None else "idle"
                    break
                await asyncio.sleep(0.5)
            return {
                "success": True,
                "method": "ECP",
                "cec_attempted": True,
                "playback_stopped": playback_stopped,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Roku power command failed: {e}")

    # Fallback to ECP if Lounge is not active and command matches play/pause/next/previous
    if api is None or not api.connected():
        if command in ("play", "pause", "next", "previous"):
            try:
                if command == "play":
                    ecp_cmd = "Play"
                elif command == "pause":
                    ecp_cmd = "Pause"
                elif command == "next":
                    ecp_cmd = "Fwd"
                elif command == "previous":
                    ecp_cmd = "Rev"

                url = f"http://{ip}:8060/keypress/{ecp_cmd}"
                async with aiohttp.ClientSession() as session:
                    await session.post(url, timeout=3)
                return {"success": True, "method": "ECP"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"ECP remote failed: {e}")
        raise HTTPException(status_code=400, detail="YouTube Lounge connection is not active on this device.")

    try:
        if command == "play":
            await api.play()
        elif command == "pause":
            await api.pause()
        elif command == "next":
            await api.next()
        elif command == "previous":
            await api.previous()
        elif command == "seek":
            await api.seek_to(float(value))
        return {"success": True, "method": "Lounge"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/devices/{ip}/play-last")
async def api_play_last(ip: str):
    last_played = db_get_last_played_for_device(ip)
    if not last_played:
        raise HTTPException(status_code=404, detail="No playback history found for this device.")

    if is_plex_video_id(last_played["video_id"]):
        session_info = active_sessions.get(ip)
        if session_info:
            session_info["resuming"] = True
            request_fast_monitoring(ip)
        try:
            await resume_plex_media(
                ip,
                get_plex_client_id(),
                db_get_setting("plex_token"),
                last_played["video_id"],
                last_played["position"],
            )
        except PlexError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            if session_info:
                session_info["resuming"] = False
                request_fast_monitoring(ip)
    else:
        asyncio.create_task(resume_playback_background(ip, last_played["video_id"], last_played["position"]))
    return {
        "success": True,
        "video_id": last_played["video_id"],
        "video_title": last_played["video_title"],
        "position": last_played["position"]
    }

class SettingsSchema(BaseModel):
    plex_token: str = ""

@app.get("/api/settings")
async def api_get_settings():
    return {
        "plex_connected": bool(db_get_setting("plex_token")),
        "plex_account_name": db_get_setting("plex_account_name", ""),
    }

@app.post("/api/settings")
async def api_post_settings(payload: SettingsSchema):
    if payload.plex_token:
        account = await plex_get_account(get_plex_client_id(), payload.plex_token)
        db_set_setting("plex_account_name", account["name"])
    db_set_setting("plex_token", payload.plex_token)
    invalidate_resource_cache()
    if payload.plex_token:
        asyncio.create_task(refresh_plex_catalog_background(payload.plex_token))
    return {"success": True}


@app.post("/api/plex/auth/start")
async def api_plex_auth_start():
    try:
        pin = await start_pin_auth(get_plex_client_id())
    except PlexError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    pending_plex_pins[pin["id"]] = pin["expires_at"]
    return {"id": pin["id"], "auth_url": pin["auth_url"]}


@app.get("/api/plex/auth/status/{pin_id}")
async def api_plex_auth_status(pin_id: str):
    expires_at = pending_plex_pins.get(pin_id)
    if not expires_at:
        raise HTTPException(status_code=404, detail="Unknown Plex authorization request.")
    if time.time() > expires_at:
        pending_plex_pins.pop(pin_id, None)
        raise HTTPException(status_code=410, detail="Plex authorization expired. Start again.")
    try:
        token = await poll_pin_auth(get_plex_client_id(), pin_id)
        if not token:
            return {"connected": False}
        account = await plex_get_account(get_plex_client_id(), token)
    except PlexError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    db_set_setting("plex_token", token)
    db_set_setting("plex_account_name", account["name"])
    pending_plex_pins.pop(pin_id, None)
    invalidate_resource_cache()
    asyncio.create_task(refresh_plex_catalog_background(token))
    return {"connected": True, "account_name": account["name"]}


@app.delete("/api/plex/auth")
async def api_plex_auth_disconnect():
    db_set_setting("plex_token", "")
    db_set_setting("plex_account_name", "")
    invalidate_resource_cache()
    return {"success": True}


@app.get("/api/plex/catalog")
async def api_plex_catalog():
    token = db_get_setting("plex_token")
    if not token:
        raise HTTPException(status_code=401, detail="Plex account is not connected.")
    try:
        return await plex_get_kid_catalog(get_plex_client_id(), token)
    except PlexError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/plex/catalog/refresh")
async def api_refresh_plex_catalog():
    token = db_get_setting("plex_token")
    if not token:
        raise HTTPException(status_code=401, detail="Plex account is not connected.")
    try:
        return await plex_get_kid_catalog(get_plex_client_id(), token, force=True)
    except PlexError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/plex/catalog/play")
async def api_play_plex_catalog_item(payload: PlexCatalogPlaySchema):
    if payload.ip not in active_sessions:
        raise HTTPException(status_code=404, detail="Device not found/monitored")
    token = db_get_setting("plex_token")
    if not token:
        raise HTTPException(status_code=401, detail="Plex account is not connected.")
    session_info = active_sessions[payload.ip]
    session_info["resuming"] = True
    request_fast_monitoring(payload.ip)
    try:
        media = await resolve_plex_catalog_media(get_plex_client_id(), token, payload.video_id)
        result = await resume_plex_media(
            payload.ip,
            get_plex_client_id(),
            token,
            media["video_id"],
            media["position"],
        )
        return {"success": True, **media, "played_title": result["title"]}
    except PlexError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        session_info["resuming"] = False
        request_fast_monitoring(payload.ip)


@app.get("/api/plex/art/{server_id}/{rating_key}")
async def api_plex_art(server_id: str, rating_key: str):
    token = db_get_setting("plex_token")
    if not token:
        raise HTTPException(status_code=401, detail="Plex account is not connected.")
    try:
        body, content_type = await plex_get_art(get_plex_client_id(), token, server_id, rating_key)
    except PlexError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(content=body, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("ROKU_CONTROL_HOST", "0.0.0.0"),
        port=int(os.getenv("ROKU_CONTROL_PORT", "8001")),
        reload=False,
    )
