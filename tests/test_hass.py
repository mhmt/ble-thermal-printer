"""Home Assistant bridge, Ingress mode, API-only listener and token endpoints (no printer, no HA)."""
from __future__ import annotations

import asyncio
import http.client
import json
import logging
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from test_api import ApiManager

from ble_thermal_printer import config, hass, server, tokens
from ble_thermal_printer.ble import PrinterError

TOKEN = "test-supervisor-token"


class StatesServer:
    """Records POST /api/states/<entity_id> like Home Assistant's REST API."""

    def __init__(self):
        self.posts: list[tuple[str, dict, str]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append((self.path, body, self.headers.get("Authorization")))
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class BridgeTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.ERROR)
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_patch = mock.patch.object(config, "CONFIG_PATH", Path(self.tmp.name) / "config.json")
        self.cfg_patch.start()
        self.states = StatesServer()
        self.mgr = ApiManager()

    def tearDown(self):
        self.states.close()
        self.cfg_patch.stop()
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def run_bridge(self, events: list[dict], token: str = TOKEN, expect: int | None = None):
        """Fake HA WebSocket server: sends `events` after subscribe, returns (fired events, received msgs)."""
        from websockets.asyncio.server import serve

        expect = len(events) if expect is None else expect
        fired: list[dict] = []
        received: list[dict] = []

        async def ha(ws):
            await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.11.0"}))
            auth = json.loads(await ws.recv())
            received.append(auth)
            if auth.get("access_token") != TOKEN:
                await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
                return
            await ws.send(json.dumps({"type": "auth_ok"}))
            sub = json.loads(await ws.recv())
            received.append(sub)
            await ws.send(json.dumps({"id": sub["id"], "type": "result", "success": True, "result": None}))
            for data in events:
                await ws.send(json.dumps({"id": sub["id"], "type": "event", "event": {
                    "event_type": hass.EVENT_PRINT, "data": data, "context": {"user_id": None}}}))
            async for raw in ws:
                msg = json.loads(raw)
                received.append(msg)
                if msg.get("type") == "fire_event":
                    fired.append(msg)
                    await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": True}))

        async def main():
            async with serve(ha, "127.0.0.1", 0) as srv:
                port = srv.sockets[0].getsockname()[1]
                bridge = hass.HassBridge(self.mgr, f"ws://127.0.0.1:{port}/api/websocket", token)
                bridge.rest = self.states.url
                task = asyncio.create_task(bridge.run())
                for _ in range(100):
                    if len(fired) >= expect and self.states.posts:
                        break
                    await asyncio.sleep(0.05)
                task.cancel()
            return fired, received

        return asyncio.run(main())

    def test_rest_base(self):
        self.assertEqual(hass.rest_base("ws://supervisor/core/websocket"), "http://supervisor/core/api")
        self.assertEqual(hass.rest_base("ws://ha.local:8123/api/websocket"), "http://ha.local:8123/api")
        self.assertEqual(hass.rest_base("wss://ha.example/api/websocket"), "https://ha.example/api")

    def test_print_events(self):
        fired, received = self.run_bridge([
            {"kind": "text", "text": "hi", "width_mm": 50, "height_mm": 16, "request_id": "r1"},
            {"kind": "evil", "request_id": 7},
            {"kind": "text", "text": "x", "copies": 50},
        ])
        self.assertEqual(received[0], {"type": "auth", "access_token": TOKEN})
        self.assertEqual(received[1]["type"], "subscribe_events")
        self.assertEqual(received[1]["event_type"], "label_printer_print")
        by_id = {f["event_data"]["request_id"]: f for f in fired}
        self.assertEqual(by_id["r1"]["event_type"], "label_printer_printed")
        self.assertEqual(by_id["r1"]["event_data"], {"request_id": "r1", "kind": "text", "copies": 1,
                                                     "width_mm": 50.0, "height_mm": 16.0})
        self.assertEqual(by_id[7]["event_type"], "label_printer_print_failed")
        self.assertEqual(by_id[7]["event_data"]["error"]["params"]["field"], "kind")
        self.assertEqual(by_id[None]["event_data"]["error"]["params"]["field"], "copies")
        self.assertEqual([c for c in self.mgr.calls if c[0] == "print"],
                         [("print", 50.0, 16.0, 1, False, "Home Assistant")])

    def test_printer_offline(self):
        self.mgr.fail_with = PrinterError("not_connected", "Printer is not connected")
        fired, _ = self.run_bridge([{"kind": "text", "text": "x", "request_id": "a"}])
        self.assertEqual(fired[0]["event_type"], "label_printer_print_failed")
        self.assertEqual(fired[0]["event_data"]["error"]["code"], "not_connected")

    def test_sensor_published(self):
        self.run_bridge([], expect=0)
        path, body, auth = self.states.posts[0]
        self.assertEqual(path, "/api/states/sensor.label_printer")
        self.assertEqual(auth, f"Bearer {TOKEN}")
        self.assertEqual(body["state"], "ready")
        self.assertEqual(body["attributes"]["printer"], "P1_TEST")
        self.assertIn("ready", body["attributes"]["options"])

    def test_bad_token_does_not_subscribe(self):
        fired, received = self.run_bridge([], token="wrong", expect=0)
        self.assertFalse(any(m.get("type") == "subscribe_events" for m in received))
        self.assertEqual(self.states.posts, [])

    def test_sensor_states(self):
        cases = [({"phase": "connected", "status": {"ready": True, "errors": []}}, "ready"),
                 ({"phase": "connected", "status": {"ready": False, "errors": ["cover_open"]}}, "not_ready"),
                 ({"phase": "printing"}, "printing"), ({"phase": "connecting"}, "connecting"),
                 ({"phase": "lost"}, "disconnected"), ({"phase": "idle"}, "disconnected")]
        for snap, state in cases:
            self.assertEqual(hass.sensor_state(snap)[0], state, snap)


class ListenerTest(unittest.TestCase):
    """Ingress (trusted proxy) and the API-only listener."""

    def setUp(self):
        logging.disable(logging.ERROR)
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_patch = mock.patch.object(config, "CONFIG_PATH", Path(self.tmp.name) / "config.json")
        self.cfg_patch.start()
        self.mgr = ApiManager()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.cfg_patch.stop()
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def start(self, handler) -> int:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.servers.append(httpd)
        return httpd.server_address[1]

    def call(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, raw

    def test_ingress_accepts_trusted_proxy_with_any_host(self):
        port = self.start(server.make_handler(self.mgr, None, trusted_proxy="127.0.0.1"))
        ha = {"Host": "homeassistant.local:8123", "Origin": "http://homeassistant.local:8123"}
        self.assertEqual(self.call(port, "GET", "/", headers=ha)[0], 200)
        self.assertEqual(self.call(port, "POST", "/api/preview", {"kind": "text", "text": "x"}, ha)[0], 200)
        # cross-site requests are still blocked behind the proxy
        evil = {**ha, "Origin": "https://evil.example"}
        self.assertEqual(self.call(port, "POST", "/api/print", {"kind": "text"}, evil)[0], 403)

    def test_ingress_rejects_other_addresses(self):
        port = self.start(server.make_handler(self.mgr, None, trusted_proxy="172.30.32.2"))
        self.assertEqual(self.call(port, "GET", "/", headers={"Host": "x"})[0], 403)
        self.assertEqual(self.call(port, "GET", "/api/state", headers={"Host": "x"})[0], 403)

    def test_api_only_listener(self):
        _, secret = tokens.create("ha")
        port = self.start(server.make_handler(self.mgr, None, api_only=True))
        lan = {"Host": "homeassistant.local:8765"}
        self.assertEqual(self.call(port, "GET", "/", headers=lan)[0], 404)
        self.assertEqual(self.call(port, "GET", "/api/state", headers=lan)[0], 404)
        self.assertEqual(self.call(port, "POST", "/api/tokens/create", {"name": "x"}, lan)[0], 404)
        self.assertEqual(self.call(port, "GET", "/api/v1/status", headers=lan)[0], 401)
        status, _ = self.call(port, "GET", "/api/v1/status", headers={**lan, "Authorization": f"Bearer {secret}"})
        self.assertEqual(status, 200)
        self.assertEqual(tokens.list_tokens()[0].name, "ha")  # nothing created through the API port

    def test_token_endpoints(self):
        port = self.start(server.make_handler(self.mgr))
        local = {"Host": "127.0.0.1"}
        status, raw = self.call(port, "POST", "/api/tokens/create", {"name": "home-assistant", "scope": "read"}, local)
        self.assertEqual(status, 200)
        j = json.loads(raw)
        self.assertTrue(j["secret"].startswith("btp_"))
        self.assertEqual(j["token"]["scope"], "read")
        self.assertEqual(tokens.verify(j["secret"]).name, "home-assistant")
        status, raw = self.call(port, "GET", "/api/tokens", headers=local)
        listed = json.loads(raw)["tokens"]
        self.assertEqual([k["name"] for k in listed], ["home-assistant"])
        self.assertNotIn("hash", listed[0])
        status, raw = self.call(port, "POST", "/api/tokens/create", {"name": "home-assistant"}, local)
        self.assertEqual((status, json.loads(raw)["error"]["code"]), (400, "token_exists"))
        status, raw = self.call(port, "POST", "/api/tokens/create", {"name": "x", "rate_per_min": 0}, local)
        self.assertEqual(json.loads(raw)["error"]["params"]["field"], "rate_per_min")
        status, raw = self.call(port, "POST", "/api/tokens/revoke", {"id": listed[0]["id"]}, local)
        self.assertEqual((status, json.loads(raw)["tokens"]), (200, []))


if __name__ == "__main__":
    unittest.main()
