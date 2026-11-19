"""Tests for the token-authenticated /api/v1 endpoints (no printer needed)."""
from __future__ import annotations

import http.client
import json
import logging
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from test_server import FakeManager, png_data_url

from ble_thermal_printer import config, httpguard, server, tokens
from ble_thermal_printer.ble import PrinterError

FUNNEL_HOST = "pi.example-tailnet.ts.net"


class ApiManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.phase = "connected"
        self.fail_with: Exception | None = None

    def snapshot(self) -> dict:
        return {"phase": self.phase, "device": {"name": "P1_TEST", "address": "AA"},
                "status": {"errors": [], "ready": True}}

    async def print(self, img, w_mm, h_mm, density, copies, *, connect=True, source=None):
        if self.fail_with:
            raise self.fail_with
        self.calls.append(("print", w_mm, h_mm, copies, connect, source))


class ApiTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.ERROR)  # rejected and failing requests are logged on purpose
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_patch = mock.patch.object(config, "CONFIG_PATH", Path(self.tmp.name) / "config.json")
        self.cfg_patch.start()
        self.mgr = ApiManager()
        hosts = httpguard.allowed_hosts("127.0.0.1", [FUNNEL_HOST])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.mgr, hosts, frozenset({FUNNEL_HOST})))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        _, self.printer = tokens.create("ha")
        _, self.reader = tokens.create("viewer", scope="read")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.cfg_patch.stop()
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def call(self, method, path, body=None, token=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r, json.loads(raw)
        except ValueError:
            return r, raw

    def prints(self):
        return [c for c in self.mgr.calls if c[0] == "print"]

    # --- auth ----------------------------------------------------------------
    def test_requires_valid_token(self):
        for token in (None, "btp_wrong", "nope"):
            r, j = self.call("GET", "/api/v1/status", token=token)
            self.assertEqual(r.status, 401, token)
            self.assertEqual(j["error"]["code"], "unauthorized")
            self.assertIn("Bearer", r.getheader("WWW-Authenticate"))
        r, _ = self.call("GET", "/api/v1/status", headers={"Authorization": f"Basic {self.printer}"})
        self.assertEqual(r.status, 401)

    def test_auth_checked_before_body_is_read(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/v1/print", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "5000000")
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual(r.status, 401)
        conn.close()

    def test_revoked_token_rejected_immediately(self):
        r, _ = self.call("GET", "/api/v1/status", token=self.printer)
        self.assertEqual(r.status, 200)
        tokens.revoke("ha")
        r, _ = self.call("GET", "/api/v1/status", token=self.printer)
        self.assertEqual(r.status, 401)

    def test_read_scope(self):
        r, j = self.call("GET", "/api/v1/status", token=self.reader)
        self.assertEqual(r.status, 200)
        self.assertEqual(j["printer"], {"phase": "connected", "connected": True, "ready": True, "errors": [],
                                        "name": "P1_TEST", "label_size_mm": [40.0, 30.0]})
        r, png = self.call("POST", "/api/v1/preview", {"kind": "text", "text": "hi"}, token=self.reader)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "image/png")
        r, j = self.call("POST", "/api/v1/print", {"kind": "text", "text": "hi"}, token=self.reader)
        self.assertEqual(r.status, 403)
        self.assertEqual(j["error"]["code"], "insufficient_scope")
        self.assertEqual(self.prints(), [])

    def test_routing(self):
        r, _ = self.call("GET", "/api/v1/nope", token=self.printer)
        self.assertEqual(r.status, 404)
        r, _ = self.call("GET", "/api/v1/print", token=self.printer)
        self.assertEqual(r.status, 405)
        r, _ = self.call("GET", "/api/v1/status", token=self.printer, headers={"Host": "evil.example"})
        self.assertEqual(r.status, 403)

    # --- printing --------------------------------------------------------------
    def test_print(self):
        r, j = self.call("POST", "/api/v1/print", {"kind": "qr", "data": "x", "width_mm": 50, "height_mm": 16,
                                                   "copies": 2}, token=self.printer)
        self.assertEqual(r.status, 200, j)
        self.assertEqual(j["printed"], {"kind": "qr", "copies": 2, "width_mm": 50.0, "height_mm": 16.0})
        self.assertEqual(self.prints(), [("print", 50.0, 16.0, 2, False, "API: ha")])

    def test_print_image(self):
        r, _ = self.call("POST", "/api/v1/print", {"kind": "image", "image": png_data_url()}, token=self.printer)
        self.assertEqual(r.status, 200)

    def test_api_copy_limit(self):
        r, j = self.call("POST", "/api/v1/print", {"kind": "text", "text": "x", "copies": 11}, token=self.printer)
        self.assertEqual(r.status, 400)
        self.assertEqual(j["error"]["params"]["field"], "copies")

    def test_printer_not_connected_fails_fast(self):
        self.mgr.phase = "lost"
        r, j = self.call("POST", "/api/v1/print", {"kind": "text", "text": "x"}, token=self.printer)
        self.assertEqual(r.status, 503)
        self.assertEqual(j["error"]["code"], "not_connected")
        self.assertEqual(self.prints(), [])

    def test_printer_not_ready(self):
        self.mgr.fail_with = PrinterError("not_ready", "Printer not ready: cover open", errors=["cover_open"])
        r, j = self.call("POST", "/api/v1/print", {"kind": "text", "text": "x"}, token=self.printer)
        self.assertEqual(r.status, 503)
        self.assertEqual(j["error"], {"code": "not_ready", "params": {"errors": ["cover_open"]},
                                      "text": "Printer not ready: cover open"})

    def test_unexpected_error_is_not_leaked(self):
        self.mgr.fail_with = RuntimeError("secret internal detail")
        r, j = self.call("POST", "/api/v1/print", {"kind": "text", "text": "x"}, token=self.printer)
        self.assertEqual(r.status, 500)
        self.assertNotIn("secret", json.dumps(j))

    # --- idempotency, quota, rate limit ------------------------------------------
    def test_idempotency_key(self):
        body = {"kind": "text", "text": "order 42"}
        key = {"Idempotency-Key": "order-42"}
        r1, j1 = self.call("POST", "/api/v1/print", body, token=self.printer, headers=key)
        r2, j2 = self.call("POST", "/api/v1/print", body, token=self.printer, headers=key)
        self.assertEqual((r1.status, r2.status), (200, 200))
        self.assertEqual(j1, j2)
        self.assertEqual(r2.getheader("Idempotent-Replayed"), "true")
        self.assertEqual(len(self.prints()), 1)
        r3, j3 = self.call("POST", "/api/v1/print", {"kind": "text", "text": "other"}, token=self.printer, headers=key)
        self.assertEqual(r3.status, 422)
        self.assertEqual(j3["error"]["code"], "idempotency_key_reused")
        # keys are per token
        _, other = tokens.create("other")
        r4, _ = self.call("POST", "/api/v1/print", body, token=other, headers=key)
        self.assertEqual(r4.status, 200)
        self.assertEqual(len(self.prints()), 2)

    def test_failed_request_can_be_retried_with_same_key(self):
        body, key = {"kind": "text", "text": "x"}, {"Idempotency-Key": "k1"}
        self.mgr.phase = "lost"
        r, _ = self.call("POST", "/api/v1/print", body, token=self.printer, headers=key)
        self.assertEqual(r.status, 503)
        self.mgr.phase = "connected"
        r, _ = self.call("POST", "/api/v1/print", body, token=self.printer, headers=key)
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.getheader("Idempotent-Replayed"))

    def test_daily_quota(self):
        _, small = tokens.create("small", daily_labels=3)
        body = {"kind": "text", "text": "x", "copies": 2}
        r, _ = self.call("POST", "/api/v1/print", body, token=small)
        self.assertEqual(r.status, 200)
        r, j = self.call("POST", "/api/v1/print", body, token=small)
        self.assertEqual(r.status, 429)
        self.assertEqual(j["error"]["code"], "quota_exceeded")
        self.assertEqual(j["error"]["params"], {"used": 2, "limit": 3})
        # a failed print doesn't use quota
        self.mgr.fail_with = PrinterError("not_ready", "x")
        r, _ = self.call("POST", "/api/v1/print", {"kind": "text", "text": "x"}, token=small)
        self.assertEqual(r.status, 503)
        self.mgr.fail_with = None
        r, _ = self.call("POST", "/api/v1/print", {"kind": "text", "text": "x"}, token=small)
        self.assertEqual(r.status, 200)

    def test_rate_limit(self):
        _, slow = tokens.create("slow", rate_per_min=2)
        for _ in range(2):
            r, _ = self.call("GET", "/api/v1/status", token=slow)
            self.assertEqual(r.status, 200)
        r, j = self.call("GET", "/api/v1/status", token=slow)
        self.assertEqual(r.status, 429)
        self.assertEqual(j["error"]["code"], "rate_limited")
        self.assertGreaterEqual(int(r.getheader("Retry-After")), 1)
        r, _ = self.call("GET", "/api/v1/status", token=self.printer)  # other tokens unaffected
        self.assertEqual(r.status, 200)

    # --- public (internet) access ---------------------------------------------------
    def test_public_host_only_reaches_api(self):
        for headers in ({"Host": FUNNEL_HOST}, {"Tailscale-Funnel-Request": "?1"}):
            with self.subTest(headers=headers):
                r, _ = self.call("GET", "/", headers=headers)
                self.assertEqual(r.status, 404)
                r, _ = self.call("GET", "/api/state", headers=headers)
                self.assertEqual(r.status, 404)
                r, _ = self.call("POST", "/api/connect", {}, headers=headers)
                self.assertEqual(r.status, 404)
                r, _ = self.call("GET", "/api/v1/status", token=self.printer, headers=headers)
                self.assertEqual(r.status, 200)
        self.assertEqual(self.mgr.calls, [])
        r, _ = self.call("GET", "/")  # the LAN/local name still serves the UI
        self.assertEqual(r.status, 200)


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.cfg_patch = mock.patch.object(config, "CONFIG_PATH", self.path)
        self.cfg_patch.start()

    def tearDown(self):
        self.cfg_patch.stop()
        self.tmp.cleanup()

    def test_secret_is_not_stored(self):
        tok, secret = tokens.create("ha")
        self.assertTrue(secret.startswith("btp_"))
        self.assertNotIn(secret, self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(tokens.verify(secret), tok)
        self.assertIsNone(tokens.verify(secret + "x"))

    def test_validation(self):
        tokens.create("ha")
        for kwargs, code in (({"name": "ha"}, "token_exists"), ({"name": ""}, "token_bad_name"),
                             ({"name": "x", "scope": "admin"}, "token_bad_scope"),
                             ({"name": "y", "rate_per_min": 0}, "token_bad_limit")):
            with self.assertRaises(tokens.TokenError) as cm:
                tokens.create(**kwargs)
            self.assertEqual(cm.exception.code, code)

    def test_other_config_keys_kept(self):
        config.save({"label_size": "50x16"})
        tokens.create("ha")
        tokens.revoke("ha")
        self.assertEqual(config.load(), {"label_size": "50x16", "api_tokens": []})


if __name__ == "__main__":
    unittest.main()
