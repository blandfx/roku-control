import asyncio
import json
import os
import time
import urllib.parse
import xml.etree.ElementTree as ET

import aiohttp


PLEX_TV = "https://plex.tv"
PLEX_PRODUCT = "Roku Media Monitor"
PLEX_VERSION = "1.0"
PLEX_CLIENT_PORT = 8324
PLEX_MOVIE_LIBRARY = os.getenv("PLEX_MOVIE_LIBRARY", "Kid Movies")
PLEX_TV_LIBRARY = os.getenv("PLEX_TV_LIBRARY", "Kid TV Shows")

_resource_cache = {"token": None, "expires": 0.0, "resources": []}
_connection_cache = {}
_art_cache = {}
_catalog_cache = {"token": None, "expires": 0.0, "result": None}


class PlexError(RuntimeError):
    pass


def is_plex_video_id(video_id):
    return bool(video_id) and (video_id.startswith("plex:") or video_id.startswith("plex_"))


def make_plex_video_id(server_id, rating_key):
    return f"plex:{server_id}:{rating_key}"


def make_plex_show_id(server_id, rating_key):
    return f"plex-show:{server_id}:{rating_key}"


def parse_plex_show_id(video_id):
    parts = (video_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "plex-show" or not parts[1] or not parts[2]:
        raise PlexError("Invalid Plex show identifier.")
    return parts[1], parts[2]


def parse_plex_video_id(video_id):
    if not video_id or not video_id.startswith("plex:"):
        raise PlexError("This Plex history entry predates reliable resume. Play it once so it can be identified again.")
    parts = video_id.split(":", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        raise PlexError("Invalid Plex media identifier.")
    return parts[1], parts[2]


def plex_headers(client_id, token=None, target_id=None, accept="application/xml"):
    headers = {
        "Accept": accept,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Version": PLEX_VERSION,
        "X-Plex-Platform": "Linux",
        "X-Plex-Platform-Version": "1",
        "X-Plex-Device": "PC",
        "X-Plex-Device-Name": PLEX_PRODUCT,
        "X-Plex-Provides": "controller",
    }
    if token:
        headers["X-Plex-Token"] = token
    if target_id:
        headers["X-Plex-Target-Client-Identifier"] = target_id
    return headers


async def _response_data(response):
    text = await response.text()
    if response.status not in (200, 201, 204):
        raise PlexError(f"Plex returned HTTP {response.status}: {text[:200]}")
    return text


async def start_pin_auth(client_id):
    headers = plex_headers(client_id, accept="application/json")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{PLEX_TV}/api/v2/pins", params={"strong": "true"}, headers=headers, timeout=10
        ) as response:
            data = await response.json(content_type=None)
            if response.status not in (200, 201):
                raise PlexError(data.get("error", f"Plex returned HTTP {response.status}"))

    code = data.get("code")
    pin_id = data.get("id")
    if not code or not pin_id:
        raise PlexError("Plex did not return an authorization code.")
    query = urllib.parse.urlencode({
        "clientID": client_id,
        "context[device][product]": PLEX_PRODUCT,
        "context[device][version]": PLEX_VERSION,
        "context[device][platform]": "Linux",
        "context[device][platformVersion]": "1",
        "context[device][device]": "PC",
        "context[device][deviceName]": PLEX_PRODUCT,
        "code": code,
    })
    return {
        "id": str(pin_id),
        "code": code,
        "auth_url": f"https://app.plex.tv/auth/#!?{query}",
        "expires_at": time.time() + 300,
    }


async def poll_pin_auth(client_id, pin_id):
    headers = plex_headers(client_id, accept="application/json")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{PLEX_TV}/api/v2/pins/{pin_id}", headers=headers, timeout=10) as response:
            data = await response.json(content_type=None)
            if response.status != 200:
                raise PlexError(data.get("error", f"Plex returned HTTP {response.status}"))
    return data.get("authToken")


async def get_account(client_id, token):
    headers = plex_headers(client_id, token, accept="application/json")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{PLEX_TV}/api/v2/user", headers=headers, timeout=10) as response:
            if response.status != 200:
                raise PlexError("The Plex authorization is invalid or expired.")
            data = await response.json(content_type=None)
    return {
        "name": data.get("friendlyName") or data.get("username") or data.get("title") or "Plex user",
        "username": data.get("username", ""),
    }


def invalidate_resource_cache():
    _resource_cache.update({"token": None, "expires": 0.0, "resources": []})
    _connection_cache.clear()
    _catalog_cache.update({"token": None, "expires": 0.0, "result": None})


def _catalog_item(server_id, library_title, item):
    rating_key = item.get("ratingKey")
    media_type = item.get("type")
    if not rating_key or media_type not in ("movie", "show"):
        return None
    if media_type == "show":
        title = item.get("title") or "Plex TV Show"
        year = item.get("year")
        subtitle = f"{library_title} • {year}" if year else library_title
        search_text = " ".join(str(part or "") for part in (title, year, library_title))
        video_id = make_plex_show_id(server_id, rating_key)
    else:
        title = format_metadata_title(item)
        year = item.get("year")
        subtitle = f"{library_title} • {year}" if year else library_title
        search_text = " ".join(str(part or "") for part in (item.get("title"), year, library_title))
        video_id = make_plex_video_id(server_id, rating_key)
    return {
        "video_id": video_id,
        "title": title,
        "subtitle": subtitle,
        "type": media_type,
        "library": library_title,
        "thumbnail_url": f"api/plex/art/{server_id}/{rating_key}",
        "search_text": search_text.casefold(),
    }


async def _get_server_json(session, url, headers, params=None):
    async with session.get(url, headers=headers, params=params, timeout=15) as response:
        text = await _response_data(response)
    return json.loads(text)


async def get_kid_catalog(client_id, account_token, force=False):
    if not account_token:
        raise PlexError("Connect the Plex account before loading its media library.")
    now = time.monotonic()
    if (
        not force
        and _catalog_cache["token"] == account_token
        and now < _catalog_cache["expires"]
        and _catalog_cache["result"] is not None
    ):
        return _catalog_cache["result"]

    target_libraries = {PLEX_MOVIE_LIBRARY: "1", PLEX_TV_LIBRARY: "2"}
    resources = await get_server_resources(client_id, account_token)
    catalog = []
    libraries_found = []
    errors = []
    for resource in resources:
        try:
            base_url = await resolve_server_connection(client_id, resource)
            headers = plex_headers(client_id, resource["token"], accept="application/json")
            async with aiohttp.ClientSession() as session:
                sections_data = await _get_server_json(session, f"{base_url}/library/sections", headers)
                sections = sections_data.get("MediaContainer", {}).get("Directory", [])
                for section in sections:
                    library_title = section.get("title")
                    media_type = target_libraries.get(library_title)
                    section_key = section.get("key")
                    if not media_type or not section_key:
                        continue
                    library_ref = f'{resource["name"]}: {library_title}'
                    if library_ref not in libraries_found:
                        libraries_found.append(library_ref)
                    start = 0
                    page_size = 500
                    while True:
                        data = await _get_server_json(
                            session,
                            f"{base_url}/library/sections/{urllib.parse.quote(str(section_key), safe='')}/all",
                            headers,
                            params={
                                "type": media_type,
                                "X-Plex-Container-Start": str(start),
                                "X-Plex-Container-Size": str(page_size),
                            },
                        )
                        items = data.get("MediaContainer", {}).get("Metadata", [])
                        catalog.extend(
                            entry for entry in (
                                _catalog_item(resource["id"], library_title, item) for item in items
                            ) if entry
                        )
                        if len(items) < page_size:
                            break
                        start += len(items)
        except (PlexError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            errors.append(f'{resource["name"]}: {exc}')

    unique = {item["video_id"]: item for item in catalog}
    items = sorted(unique.values(), key=lambda item: (item["library"], item["title"].casefold()))
    result = {
        "items": items,
        "libraries": libraries_found,
        "errors": errors,
        "refreshed_at": time.time(),
    }
    _catalog_cache.update({"token": account_token, "expires": now + 600, "result": result})
    return result


def _episode_sort_key(item):
    try:
        season = int(item.get("parentIndex") or 0)
    except (TypeError, ValueError):
        season = 0
    try:
        episode = int(item.get("index") or 0)
    except (TypeError, ValueError):
        episode = 0
    return season, episode


def choose_show_episode(items):
    episodes = sorted((item for item in items if item.get("type") == "episode"), key=_episode_sort_key)
    if not episodes:
        raise PlexError("This Plex show has no playable episodes.")
    in_progress = [
        item for item in episodes
        if int(item.get("viewOffset") or 0) > 0
        and int(item.get("viewOffset") or 0) < int(item.get("duration") or 0)
    ]
    if in_progress:
        return max(in_progress, key=lambda item: int(item.get("lastViewedAt") or 0))
    unwatched = [item for item in episodes if not int(item.get("viewCount") or 0)]
    normal_unwatched = [item for item in unwatched if _episode_sort_key(item)[0] > 0]
    if normal_unwatched:
        return normal_unwatched[0]
    if unwatched:
        return unwatched[0]
    normal_episodes = [item for item in episodes if _episode_sort_key(item)[0] > 0]
    return normal_episodes[0] if normal_episodes else episodes[0]


async def resolve_catalog_media(client_id, account_token, catalog_id):
    if catalog_id.startswith("plex-show:"):
        server_id, rating_key = parse_plex_show_id(catalog_id)
        resource = await get_server_resource(client_id, account_token, server_id)
        base_url = await resolve_server_connection(client_id, resource)
        headers = plex_headers(client_id, resource["token"], accept="application/json")
        async with aiohttp.ClientSession() as session:
            data = await _get_server_json(
                session,
                f"{base_url}/library/metadata/{urllib.parse.quote(str(rating_key), safe='')}/allLeaves",
                headers,
            )
        episode = choose_show_episode(data.get("MediaContainer", {}).get("Metadata", []))
        return {
            "video_id": make_plex_video_id(server_id, episode["ratingKey"]),
            "title": format_metadata_title(episode),
            "position": int(episode.get("viewOffset") or 0) / 1000.0,
        }

    server_id, rating_key = parse_plex_video_id(catalog_id)
    _, _, item = await get_metadata(client_id, account_token, server_id, rating_key)
    return {
        "video_id": catalog_id,
        "title": format_metadata_title(item),
        "position": int(item.get("viewOffset") or 0) / 1000.0,
    }


async def get_server_resources(client_id, account_token, force=False):
    now = time.monotonic()
    if not force and _resource_cache["token"] == account_token and now < _resource_cache["expires"]:
        return _resource_cache["resources"]

    headers = plex_headers(client_id, account_token)
    params = {"includeHttps": "1", "includeRelay": "1"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{PLEX_TV}/api/v2/resources", params=params, headers=headers, timeout=12) as response:
            text = await _response_data(response)
    resources = _parse_server_resources(text, account_token)
    _resource_cache.update({"token": account_token, "expires": now + 300, "resources": resources})
    return resources


def _parse_server_resources(text, account_token):
    root = ET.fromstring(text)
    resources = []
    nodes = list(root.findall("Device")) + list(root.findall("resource"))
    for node in nodes:
        provides = set((node.get("provides") or "").split(","))
        if node.get("product") != "Plex Media Server" and "server" not in provides:
            continue
        connections = []
        connection_nodes = list(node.findall("Connection")) + list(node.findall("./connections/connection"))
        for connection in connection_nodes:
            uri = connection.get("uri")
            if not uri and connection.get("protocol") and connection.get("address"):
                port = f':{connection.get("port")}' if connection.get("port") else ""
                uri = f'{connection.get("protocol")}://{connection.get("address")}{port}'
            if uri:
                connections.append({
                    "uri": uri.rstrip("/"),
                    "local": connection.get("local") == "1",
                    "relay": connection.get("relay") == "1",
                })
        resources.append({
            "id": node.get("clientIdentifier"),
            "name": node.get("name") or "Plex Media Server",
            "token": node.get("accessToken") or account_token,
            "owned": node.get("owned") == "1",
            "connections": connections,
        })
    return resources


async def get_server_resource(client_id, account_token, server_id):
    resources = await get_server_resources(client_id, account_token)
    resource = next((item for item in resources if item["id"] == server_id), None)
    if resource is None:
        resources = await get_server_resources(client_id, account_token, force=True)
        resource = next((item for item in resources if item["id"] == server_id), None)
    if resource is None:
        raise PlexError("That Plex server is not available to the connected Plex account.")
    return resource


def _connection_sort_key(connection):
    uri = connection["uri"]
    return (
        0 if connection["local"] else 1,
        1 if connection["relay"] else 0,
        0 if uri.startswith("https://") else 1,
    )


async def resolve_server_connection(client_id, resource, force=False):
    cached = _connection_cache.get(resource["id"])
    if cached and not force and time.monotonic() < cached["expires"]:
        return cached["uri"]

    headers = plex_headers(client_id, resource["token"])
    timeout = aiohttp.ClientTimeout(total=5, connect=3)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        connections = resource["connections"]
        if not resource["owned"]:
            # A shared server's "local" address belongs to its owner's LAN and
            # may collide with an unrelated host on the friend's LAN.
            connections = [item for item in connections if not item["local"]]
        for connection in sorted(connections, key=_connection_sort_key):
            try:
                async with session.get(f'{connection["uri"]}/identity', headers=headers) as response:
                    if response.status == 200:
                        identity = ET.fromstring(await response.text()).get("machineIdentifier")
                        if identity and identity != resource["id"]:
                            continue
                        _connection_cache[resource["id"]] = {
                            "uri": connection["uri"], "expires": time.monotonic() + 300
                        }
                        return connection["uri"]
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
    raise PlexError(f'Could not connect to Plex server "{resource["name"]}".')


async def get_local_client(ip, client_id, account_token=None):
    url = f"http://{ip}:{PLEX_CLIENT_PORT}/resources"
    headers = plex_headers(client_id, account_token)
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=3) as response:
                text = await _response_data(response)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise PlexError("The Plex app is not ready on this Roku.") from exc
    root = ET.fromstring(text)
    player = root.find("Player")
    if player is None:
        raise PlexError("The Roku did not advertise a Plex Companion player.")
    return {
        "id": player.get("machineIdentifier"),
        "title": player.get("title") or "Plex for Roku",
        "capabilities": set((player.get("protocolCapabilities") or "").split(",")),
        "base_url": f"http://{ip}:{PLEX_CLIENT_PORT}",
    }


async def poll_client_timeline(ip, client_id, account_token=None, command_id=1):
    client = await get_local_client(ip, client_id, account_token)
    headers = plex_headers(client_id, account_token, client["id"])
    params = {"wait": "0", "commandID": str(command_id)}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f'{client["base_url"]}/player/timeline/poll', params=params, headers=headers, timeout=4
        ) as response:
            text = await _response_data(response)
    root = ET.fromstring(text)
    timeline = next(
        (node for node in root.findall("Timeline") if node.get("type") == "video" and node.get("state") != "stopped"),
        None,
    )
    if timeline is None:
        return None
    return {
        "client": client,
        "state": timeline.get("state"),
        "server_id": timeline.get("machineIdentifier"),
        "rating_key": timeline.get("ratingKey"),
        "key": timeline.get("key"),
        "position_ms": int(timeline.get("time") or 0),
        "duration_ms": int(timeline.get("duration") or 0),
        "play_queue_id": timeline.get("playQueueID"),
    }


def format_metadata_title(item):
    title = item.get("title") or "Plex Media"
    media_type = item.get("type")
    if media_type == "episode":
        show = item.get("grandparentTitle")
        season = item.get("parentIndex")
        episode = item.get("index")
        if show and season is not None and episode is not None:
            title = f"{show} - S{int(season):02d}E{int(episode):02d} - {title}"
    elif media_type == "movie" and item.get("year"):
        title = f'{title} ({item["year"]})'
    return title


async def get_metadata(client_id, account_token, server_id, rating_key):
    resource = await get_server_resource(client_id, account_token, server_id)
    base_url = await resolve_server_connection(client_id, resource)
    headers = plex_headers(client_id, resource["token"], accept="application/json")
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{base_url}/library/metadata/{urllib.parse.quote(str(rating_key), safe='')}",
            headers=headers,
            timeout=8,
        ) as response:
            text = await _response_data(response)
    import json
    data = json.loads(text)
    items = data.get("MediaContainer", {}).get("Metadata", [])
    if not items:
        raise PlexError("The Plex media item is no longer available.")
    return resource, base_url, items[0]


async def get_media_state(ip, client_id, account_token, command_id=1):
    timeline = await poll_client_timeline(ip, client_id, account_token, command_id)
    if not timeline or not timeline["server_id"] or not timeline["rating_key"]:
        return None
    title = f'Plex Media ({timeline["rating_key"]})'
    try:
        _, _, item = await get_metadata(client_id, account_token, timeline["server_id"], timeline["rating_key"])
        title = format_metadata_title(item)
    except PlexError:
        # Identification and position come from the local player and remain useful
        # even while the account or remote server is temporarily unavailable.
        pass
    thumbnail_url = (
        f'api/plex/art/{timeline["server_id"]}/{timeline["rating_key"]}'
        if account_token
        else "https://images.unsplash.com/photo-1594909122845-11baa439b7bf?w=400&q=80"
    )
    return {
        "state": "play" if timeline["state"] == "playing" else "pause",
        "position_ms": timeline["position_ms"],
        "duration_ms": timeline["duration_ms"],
        "video_id": make_plex_video_id(timeline["server_id"], timeline["rating_key"]),
        "title": title,
        "thumbnail_url": thumbnail_url,
    }


async def get_art(client_id, account_token, server_id, rating_key):
    cache_key = (server_id, str(rating_key))
    cached = _art_cache.get(cache_key)
    if cached and time.monotonic() < cached["expires"]:
        return cached["body"], cached["content_type"]
    resource, base_url, item = await get_metadata(client_id, account_token, server_id, rating_key)
    paths = []
    for key in ("thumb", "art", "parentThumb", "grandparentThumb"):
        path = item.get(key)
        if path and path not in paths:
            paths.append(path)
    if not paths:
        raise PlexError("No artwork is available for this item.")
    headers = plex_headers(client_id, resource["token"], accept="image/*")
    async with aiohttp.ClientSession() as session:
        for path in paths:
            async with session.get(f"{base_url}{path}", headers=headers, timeout=10) as response:
                body = await response.read()
                if response.status == 200:
                    content_type = response.headers.get("Content-Type", "image/jpeg")
                    break
        else:
            raise PlexError("Plex did not return any available artwork for this item.")
    _art_cache[cache_key] = {"body": body, "content_type": content_type, "expires": time.monotonic() + 3600}
    return body, content_type


async def _server_request(method, url, client_id, token, **kwargs):
    headers = plex_headers(client_id, token, accept="application/json")
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, timeout=10, **kwargs) as response:
            text = await _response_data(response)
            return text, response.headers.get("Content-Type", "")


async def resume_media(ip, client_id, account_token, video_id, position_seconds):
    server_id, rating_key = parse_plex_video_id(video_id)
    if not account_token:
        raise PlexError("Connect the Plex account before resuming Plex media.")
    resource = await get_server_resource(client_id, account_token, server_id)
    base_url = await resolve_server_connection(client_id, resource)

    # Start Plex first; its local Companion HTTP service exists only while the app is running.
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://{ip}:8060/launch/13535", timeout=5) as response:
            if response.status not in (200, 202, 204):
                raise PlexError(f"Roku could not launch Plex (HTTP {response.status}).")
    # Roku exposes the Companion port before Plex has finished loading its UI.
    # Commands sent during that window return success but are silently ignored.
    await asyncio.sleep(2)
    client = None
    for _ in range(20):
        try:
            client = await get_local_client(ip, client_id, account_token)
            break
        except PlexError:
            await asyncio.sleep(1)
    if client is None:
        raise PlexError("Plex launched, but its Remote Control service did not become available.")
    if "playback" not in client["capabilities"]:
        raise PlexError("Enable Remote Control in the Plex settings on this Roku.")

    key = f"/library/metadata/{rating_key}"
    queue_uri = f"server://{server_id}/com.plexapp.plugins.library{key}"
    queue_params = {
        "type": "video", "uri": queue_uri, "includeChapters": "1", "includeRelated": "1",
        "continuous": "1", "repeat": "0", "shuffle": "0",
    }
    queue_text, queue_type = await _server_request(
        "POST", f"{base_url}/playQueues", client_id, resource["token"], params=queue_params
    )
    queue_id = None
    if "json" in queue_type:
        import json
        queue_id = json.loads(queue_text).get("MediaContainer", {}).get("playQueueID")
    else:
        queue_id = ET.fromstring(queue_text).get("playQueueID")
    if not queue_id:
        raise PlexError("Plex did not create a playback queue.")

    playback_token = resource["token"]
    try:
        token_text, token_type = await _server_request(
            "GET", f"{base_url}/security/token", client_id, resource["token"],
            params={"type": "delegation", "scope": "all"},
        )
        if "json" in token_type:
            import json
            playback_token = json.loads(token_text).get("token") or playback_token
        else:
            playback_token = ET.fromstring(token_text).get("token") or playback_token
    except PlexError:
        pass

    parsed = urllib.parse.urlparse(base_url)
    params = {
        "providerIdentifier": "com.plexapp.plugins.library",
        "machineIdentifier": server_id,
        "protocol": parsed.scheme,
        "address": parsed.hostname,
        "port": str(parsed.port or (443 if parsed.scheme == "https" else 80)),
        "offset": str(max(0, int(float(position_seconds) * 1000))),
        "key": key,
        "type": "video",
        "containerKey": f"/playQueues/{queue_id}?window=100&own=1",
        "token": playback_token,
    }
    headers = plex_headers(client_id, account_token, client["id"])

    async def send_play_media(command_id):
        command_params = {**params, "commandID": str(command_id)}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f'{client["base_url"]}/player/playback/playMedia',
                params=command_params,
                headers=headers,
                timeout=10,
            ) as response:
                await _response_data(response)

    base_command_id = int(time.time() * 1000) % 1000000000
    await send_play_media(base_command_id)

    expected_ms = int(float(position_seconds) * 1000)
    last_timeline = None
    for command_id in range(2, 22):
        await asyncio.sleep(1)
        if command_id in (7, 13):
            # Retry after Plex has had more time to settle. Each command must use
            # a fresh ID or the Companion client may discard it as a duplicate.
            await send_play_media(base_command_id + command_id)
        try:
            timeline = await poll_client_timeline(ip, client_id, account_token, command_id)
        except PlexError:
            continue
        last_timeline = timeline
        if timeline and str(timeline.get("rating_key")) == str(rating_key):
            if abs(timeline["position_ms"] - expected_ms) > 15000:
                seek_params = {
                    "offset": str(expected_ms), "type": "video", "commandID": str(command_id + 100)
                }
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f'{client["base_url"]}/player/playback/seekTo',
                        params=seek_params,
                        headers=headers,
                        timeout=5,
                    ) as response:
                        await _response_data(response)
            return {"title": format_metadata_title((await get_metadata(client_id, account_token, server_id, rating_key))[2])}
    observed = last_timeline.get("rating_key") if last_timeline else "no active media"
    raise PlexError(
        f"Plex accepted the command, but media {rating_key} did not start on the Roku "
        f"(observed: {observed})."
    )
