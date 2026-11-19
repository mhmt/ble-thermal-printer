"""Home Assistant bridge: print from automations and publish the printer state.

Connects to the Home Assistant WebSocket API and
- listens for `label_printer_print` events. The event data is the same label JSON as the HTTP API
  (kind, text, data, copies, ...), plus an optional `request_id`. Each event is answered with a
  `label_printer_printed` or `label_printer_print_failed` event carrying the same request_id;
- keeps `sensor.label_printer` up to date (state and attributes) through the REST API.

Inside a Home Assistant add-on it uses the Supervisor proxy (ws://supervisor/core/websocket with
$SUPERVISOR_TOKEN). Elsewhere, pass Home Assistant's WebSocket URL and a long-lived access token
in $HASS_TOKEN. Requires the `websockets` package: pip install 'ble-thermal-printer[hass]'.

Printing follows the API rules: no automatic connect (fails if the printer is offline) and at most
api.MAX_COPIES copies per event.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from functools import partial

from . import api, config
from .manager import err_info

log = logging.getLogger("ble_thermal_printer.hass")

EVENT_PRINT = "label_printer_print"
EVENT_PRINTED = "label_printer_printed"
EVENT_FAILED = "label_printer_print_failed"
DEFAULT_ENTITY = "sensor.label_printer"
SUPERVISOR_WS = "ws://supervisor/core/websocket"
STATES = ["ready", "printing", "not_ready", "connecting", "disconnected"]
PUBLISH_INTERVAL = 2.0
BACKOFF = (2, 5, 10, 30, 60)
MAX_MESSAGE = 16 * 1024 * 1024  # events may carry an image data URL


class AuthFailed(Exception):
    pass


def rest_base(ws_url: str) -> str:
    """WebSocket URL -> REST API base.
    ws://supervisor/core/websocket -> http://supervisor/core/api
    ws://ha.local:8123/api/websocket -> http://ha.local:8123/api
    """
    base = ws_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    if base.endswith("/websocket"):
        base = base[: -len("/websocket")]
    return base if base.endswith("/api") else base + "/api"


def resolve(url: str) -> tuple[str, str]:
    """(--hass value) -> (WebSocket URL, token). 'supervisor' uses the add-on's Supervisor proxy."""
    if url == "supervisor":
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            raise SystemExit("--hass supervisor: SUPERVISOR_TOKEN is not set (homeassistant_api: true?)")
        return SUPERVISOR_WS, token
    token = os.environ.get("HASS_TOKEN", "")
    if not token:
        raise SystemExit("--hass URL needs a long-lived access token in the HASS_TOKEN environment variable")
    return url, token


def sensor_state(snapshot: dict) -> tuple[str, dict]:
    phase = snapshot.get("phase")
    status = snapshot.get("status") or {}
    errors = list(status.get("errors") or [])
    if phase == "printing":
        state = "printing"
    elif phase == "connected":
        state = "ready" if status.get("ready", True) and not errors else "not_ready"
    elif phase == "connecting":
        state = "connecting"
    else:
        state = "disconnected"
    w, h = config.get_label_size()
    attrs = {
        "friendly_name": "Label printer", "icon": "mdi:printer", "device_class": "enum", "options": STATES,
        "errors": errors, "printer": (snapshot.get("device") or {}).get("name"),
        "label_size": f"{w:g}x{h:g}",
    }
    return state, attrs


class HassBridge:
    def __init__(self, mgr, ws_url: str, token: str, entity_id: str | None = DEFAULT_ENTITY,
                 *, max_copies: int = api.MAX_COPIES):
        self.mgr = mgr
        self.ws_url = ws_url
        self.rest = rest_base(ws_url)
        self.token = token
        self.entity_id = entity_id or None
        self.max_copies = max_copies
        self._next_id = 1
        self._send_lock: asyncio.Lock | None = None  # created in _session, on the running loop

    # --- connection ----------------------------------------------------------
    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except AuthFailed as e:
                log.error("Home Assistant rejected the access token: %s", e)
                attempt = len(BACKOFF) - 1
            except Exception as e:
                log.warning("Home Assistant connection failed: %s", e or e.__class__.__name__)
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        import websockets

        self._send_lock = asyncio.Lock()
        async with websockets.connect(self.ws_url, max_size=MAX_MESSAGE, open_timeout=15) as ws:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"unexpected message {msg.get('type')!r}")
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                raise AuthFailed(msg.get("message") or msg.get("type"))
            sub_id = await self._send(ws, {"type": "subscribe_events", "event_type": EVENT_PRINT})
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") == "result" and msg.get("id") == sub_id:
                    if not msg.get("success"):
                        raise RuntimeError(f"subscribe_events failed: {msg.get('error')}")
                    break
            log.info("Connected to Home Assistant; listening for %s events", EVENT_PRINT)
            tasks: set[asyncio.Task] = set()
            publisher = asyncio.create_task(self._publish_loop()) if self.entity_id else None
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "event" and msg.get("id") == sub_id:
                        task = asyncio.create_task(self._on_print(ws, msg.get("event") or {}))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif msg.get("type") == "result" and not msg.get("success"):
                        log.warning("Home Assistant command %s failed: %s", msg.get("id"), msg.get("error"))
            finally:
                if publisher:
                    publisher.cancel()

    async def _send(self, ws, msg: dict) -> int:
        async with self._send_lock:
            msg_id = self._next_id
            self._next_id += 1
            await ws.send(json.dumps({"id": msg_id, **msg}, ensure_ascii=False))
            return msg_id

    async def _fire(self, ws, event_type: str, data: dict) -> None:
        await self._send(ws, {"type": "fire_event", "event_type": event_type, "event_data": data})

    # --- printing ------------------------------------------------------------
    async def _on_print(self, ws, event: dict) -> None:
        data = event.get("data")
        data = dict(data) if isinstance(data, dict) else {}
        request_id = data.pop("request_id", None)
        if request_id is not None and not isinstance(request_id, (str, int)):
            request_id = str(request_id)
        user = ((event.get("context") or {}).get("user_id")) or "automation"
        try:
            loop = asyncio.get_running_loop()
            img, label, lr = await loop.run_in_executor(
                None, partial(api.compose_label, data, max_copies=self.max_copies))
            await self.mgr.print(img, label.width_mm, label.height_mm, lr.density, lr.copies,
                                 connect=False, source="Home Assistant")
        except Exception as e:
            info = err_info(e)
            log.warning("Print from Home Assistant (%s) failed: %s", user, info["text"])
            await self._fire(ws, EVENT_FAILED, {"request_id": request_id, "error": info})
            return
        log.info("Printed from Home Assistant (%s): %s, %d copies", user, lr.kind, lr.copies)
        await self._fire(ws, EVENT_PRINTED, {"request_id": request_id, "kind": lr.kind, "copies": lr.copies,
                                             "width_mm": label.width_mm, "height_mm": label.height_mm})

    # --- sensor --------------------------------------------------------------
    async def _publish_loop(self) -> None:
        last = None
        loop = asyncio.get_running_loop()
        while True:
            current = sensor_state(self.mgr.snapshot())
            if current != last:
                try:
                    await loop.run_in_executor(None, self._post_state, *current)
                    last = current
                except Exception as e:
                    log.warning("Could not update %s: %s", self.entity_id, e)
            await asyncio.sleep(PUBLISH_INTERVAL)

    def _post_state(self, state: str, attrs: dict) -> None:
        req = urllib.request.Request(
            f"{self.rest}/states/{self.entity_id}", method="POST",
            data=json.dumps({"state": state, "attributes": attrs}).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()


def start(mgr, url: str, entity_id: str | None = DEFAULT_ENTITY) -> None:
    """Run the bridge on the manager's event loop (in the background)."""
    try:
        import websockets  # noqa: F401
    except ImportError:
        raise SystemExit("--hass needs the websockets package: pip install 'ble-thermal-printer[hass]'") from None
    ws_url, token = resolve(url)
    bridge = HassBridge(mgr, ws_url, token, entity_id)
    asyncio.run_coroutine_threadsafe(bridge.run(), mgr.loop)
    log.info("Home Assistant bridge: %s (sensor: %s)", ws_url, entity_id or "off")
