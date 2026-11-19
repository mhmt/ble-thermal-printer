"""HTTP-level tests for the web server's request checks and validation.

Runs a real ThreadingHTTPServer on a free port with a fake ConnectionManager, so no Bluetooth
or printer is needed:  python -m unittest discover -s tests
"""
from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from PIL import Image

from ble_thermal_printer import config, httpguard, server


class FakeManager:
    def __init__(self):
        self.calls: list[tuple] = []

    def snapshot(self) -> dict:
        return {"phase": "idle"}

    def run(self, coro, timeout: float = 120):
        import asyncio
        return asyncio.run(coro)

    async def connect(self, address=None):
        self.calls.append(("connect", address))

    async def disconnect(self):
        self.calls.append(("disconnect",))

    async def scan(self, timeout=5.0):
        self.calls.append(("scan", timeout))

    async def refresh(self):
        self.calls.append(("refresh",))

    async def print(self, img, w_mm, h_mm, density, copies):
        self.calls.append(("print", img.size, w_mm, h_mm, density, copies))

    def set_auto_reconnect(self, on):
        self.calls.append(("auto_reconnect", on))


def png_data_url(size=(40, 20), mode="L") -> str:
    buf = io.BytesIO()
    Image.new(mode, size, 255).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.WARNING)  # rejected requests are logged; keep the test output clean
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg_patch = mock.patch.object(config, "CONFIG_PATH", Path(cls.tmp.name) / "config.json")
        cls.cfg_patch.start()
        cls.mgr = FakeManager()
        hosts = httpguard.allowed_hosts("127.0.0.1", ["printer.example"])
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.mgr, hosts))
        cls.port = cls.httpd.server_address[1]
        cls.host = f"127.0.0.1:{cls.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.cfg_patch.stop()
        cls.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def setUp(self):
        self.mgr.calls.clear()

    def request(self, method, path, body=None, headers=None, json_body=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": self.host}
        if body is not None and json_body:
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data

    def error_code(self, data: bytes) -> str:
        return json.loads(data)["error"]["code"]

    # --- request checks --------------------------------------------------
    def test_page_has_security_headers(self):
        r, data = self.request("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"<html", data.lower())
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("Server"), "ble-thermal-printer")

    def test_query_string_is_ignored_for_routing(self):
        r, _ = self.request("GET", "/api/state?x=1")
        self.assertEqual(r.status, 200)

    def test_foreign_host_rejected(self):
        for host in ("evil.example", "evil.example:80", f"attacker.test:{self.port}"):
            r, data = self.request("GET", "/api/state", headers={"Host": host})
            self.assertEqual(r.status, 403, host)
            self.assertEqual(self.error_code(data), "host_not_allowed")

    def test_allowed_hosts(self):
        for host in ("localhost", f"localhost:{self.port}", f"[::1]:{self.port}", "printer.example:8765"):
            r, _ = self.request("GET", "/api/state", headers={"Host": host})
            self.assertEqual(r.status, 200, host)

    def test_post_requires_json_content_type(self):
        body = json.dumps({"kind": "text", "text": "x"}).encode()
        for ctype in (None, "text/plain;charset=UTF-8", "application/x-www-form-urlencoded"):
            headers = {"Content-Type": ctype} if ctype else {}
            r, data = self.request("POST", "/api/print", body, headers, json_body=False)
            self.assertEqual(r.status, 415, ctype)
            self.assertEqual(self.error_code(data), "bad_content_type")
        self.assertEqual(self.mgr.calls, [])

    def test_cross_origin_post_rejected(self):
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            r, data = self.request("POST", "/api/print", {"kind": "text", "text": "x"}, headers)
            self.assertEqual(r.status, 403, headers)
            self.assertEqual(self.error_code(data), "cross_site")
        self.assertEqual(self.mgr.calls, [])

    def test_same_origin_post_allowed(self):
        headers = {"Origin": f"http://{self.host}", "Sec-Fetch-Site": "same-origin"}
        r, _ = self.request("POST", "/api/print", {"kind": "text", "text": "hi"}, headers)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.mgr.calls[0][0], "print")

    def test_cross_site_navigation_to_page_allowed(self):
        r, _ = self.request("GET", "/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
        self.assertEqual(r.status, 200)
        r, _ = self.request("GET", "/api/state", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"})
        self.assertEqual(r.status, 403)

    def test_body_too_large_rejected_before_reading(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/print", skip_host=True)
        conn.putheader("Host", self.host)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(httpguard.MAX_BODY_BYTES + 1))
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual(r.status, 413)
        self.assertEqual(self.error_code(r.read()), "too_large")
        conn.close()

    def test_invalid_json(self):
        r, data = self.request("POST", "/api/print", b"{nope", {"Content-Type": "application/json"}, json_body=False)
        self.assertEqual(r.status, 400)
        self.assertEqual(self.error_code(data), "bad_json")

    # --- validation ------------------------------------------------------
    def assert_invalid(self, path, body, field):
        r, data = self.request("POST", path, body)
        self.assertEqual(r.status, 400, body)
        err = json.loads(data)["error"]
        self.assertEqual(err["code"], "invalid", err)
        self.assertEqual(err["params"]["field"], field)

    def test_label_fields_validated(self):
        cases = [
            ({"kind": "evil"}, "kind"),
            ({"kind": "text", "copies": 1000}, "copies"),
            ({"kind": "text", "copies": 1.5}, "copies"),
            ({"kind": "text", "width_mm": "abc"}, "width_mm"),
            ({"kind": "text", "width_mm": 5000}, "width_mm"),
            ({"kind": "text", "height_mm": True}, "height_mm"),
            ({"kind": "text", "density": 99}, "density"),
            ({"kind": "text", "rotate": 45}, "rotate"),
            ({"kind": "text", "text": "x" * 5000}, "text"),
            ({"kind": "text", "text": ["a"]}, "text"),
            ({"kind": "text", "font_style": "../../etc/passwd"}, "font_style"),
            ({"kind": "text", "border": "yes"}, "border"),
            ({"kind": "barcode", "data": "1", "barcode_kind": "nope"}, "barcode_kind"),
            ({"kind": "image", "image": "data:image/png;base64,@@@"}, "image"),
            ({"kind": "image", "image": "data:image/png;base64," + base64.b64encode(b"not an image").decode()}, "image"),
        ]
        for body, field in cases:
            with self.subTest(body=str(body)[:60]):
                self.assert_invalid("/api/print", body, field)
        self.assertEqual(self.mgr.calls, [])

    def test_nan_rejected(self):
        r, data = self.request("POST", "/api/preview", b'{"kind":"text","width_mm":NaN}',
                               {"Content-Type": "application/json"}, json_body=False)
        self.assertEqual(r.status, 400)
        self.assertEqual(json.loads(data)["error"]["params"]["field"], "width_mm")

    def test_body_must_be_object(self):
        self.assert_invalid("/api/print", [1, 2], "body")

    def test_image_pixel_limit(self):
        url = png_data_url((7000, 5000), mode="1")  # 35 MP, small file
        self.assert_invalid("/api/preview", {"kind": "image", "image": url}, "image")

    def test_preview_and_print_ok(self):
        r, data = self.request("POST", "/api/preview", {"kind": "text", "text": "hi", "width_mm": 50, "height_mm": 16})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "image/png")
        self.assertEqual(Image.open(io.BytesIO(data)).size, (384, 128))

        r, _ = self.request("POST", "/api/print", {"kind": "qr", "data": "https://example.com", "width_mm": 40,
                                                   "height_mm": 30, "copies": 3, "density": 8, "font_size": None})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.mgr.calls, [("print", (320, 240), 40.0, 30.0, 8, 3)])

    def test_image_print_ok(self):
        r, _ = self.request("POST", "/api/print", {"kind": "image", "image": png_data_url(), "rotate": 90})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.mgr.calls[0][0], "print")

    def test_calibrate_preview(self):
        r, _ = self.request("POST", "/api/preview", {"kind": "calibrate", "width_mm": 50, "height_mm": 16})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("X-Margin-Px"), "0")

    def test_trim(self):
        self.assert_invalid("/api/trim", {"width_mm": 50, "height_mm": 16, "top_mm": 50}, "top_mm")
        self.assert_invalid("/api/trim", {"height_mm": 16}, "width_mm")
        r, _ = self.request("POST", "/api/trim", {"width_mm": 50, "height_mm": 16, "bottom_mm": 0.5})
        self.assertEqual(r.status, 200)
        self.assertEqual(config.get_trim(50, 16), (0.0, 0.5))

    def test_connection_endpoints(self):
        self.assert_invalid("/api/scan", {"timeout": 1000}, "timeout")
        self.assert_invalid("/api/connect", {"address": 5}, "address")
        self.assert_invalid("/api/settings", {"auto_reconnect": "on"}, "auto_reconnect")
        for path, body in (("/api/scan", {"timeout": 2}), ("/api/connect", {"address": "AA:BB"}),
                           ("/api/settings", {"auto_reconnect": False}), ("/api/disconnect", {})):
            r, _ = self.request("POST", path, body)
            self.assertEqual(r.status, 200, path)
        self.assertEqual(self.mgr.calls, [("scan", 2.0), ("connect", "AA:BB"),
                                          ("auto_reconnect", False), ("disconnect",)])

    def test_unknown_path(self):
        r, _ = self.request("POST", "/api/nope", {})
        self.assertEqual(r.status, 404)


class GuardTest(unittest.TestCase):
    def test_split_host(self):
        self.assertEqual(httpguard.split_host("Example.local:8765"), "example.local")
        self.assertEqual(httpguard.split_host("[::1]:8765"), "::1")
        self.assertEqual(httpguard.split_host("::1"), "::1")
        self.assertEqual(httpguard.split_host("10.0.0.2"), "10.0.0.2")

    def test_allowed_hosts(self):
        self.assertIsNone(httpguard.allowed_hosts("0.0.0.0", ["*"]))
        hosts = httpguard.allowed_hosts("192.168.1.20", ["Pi.Local:8765"])
        self.assertTrue({"localhost", "127.0.0.1", "::1", "192.168.1.20", "pi.local"} <= hosts)
        self.assertTrue(httpguard.allowed_hosts("0.0.0.0") > set(httpguard.LOOPBACK))

    def test_is_loopback(self):
        self.assertTrue(httpguard.is_loopback("127.0.0.1"))
        self.assertTrue(httpguard.is_loopback("localhost"))
        self.assertFalse(httpguard.is_loopback("0.0.0.0"))
        self.assertFalse(httpguard.is_loopback("192.168.1.20"))


if __name__ == "__main__":
    unittest.main()
