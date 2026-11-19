"""Local web interface on http://127.0.0.1:8765. Connection handling lives in manager.py.

Request checks (Host allow-list, JSON-only POST, Origin, body size) are in httpguard.py;
field validation is in schema.py.
"""
from __future__ import annotations

import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlsplit

from PIL import Image

from . import config, httpguard, render, schema
from .i18n import t
from .manager import ConnectionManager, err_info

log = logging.getLogger("ble_thermal_printer.server")

Image.MAX_IMAGE_PIXELS = schema.MAX_IMAGE_PIXELS  # PIL's own decompression-bomb guard
SOCKET_TIMEOUT = 30  # seconds; a slow or idle client can't hold a handler thread forever


def compose_from_json(req) -> tuple[Image.Image, render.Label, schema.LabelRequest]:
    lr = schema.label_request(req)
    if lr.kind == "calibrate":
        img, label = render.render_calibration(lr.width_mm, lr.height_mm)
        return img, label, lr
    top, bottom = config.get_trim(lr.width_mm, lr.height_mm)
    img, label = render.compose(lr.kind, width_mm=lr.width_mm, height_mm=lr.height_mm,
                                trim_top_mm=top, trim_bottom_mm=bottom, **lr.compose_kwargs)
    return img, label, lr


def make_handler(mgr: ConnectionManager, hosts: set[str] | frozenset[str] | None = frozenset(httpguard.LOOPBACK)):
    """hosts: accepted Host header names (see httpguard.allowed_hosts); None accepts any."""
    def full_state() -> dict:
        return {**mgr.snapshot(), "trims": config.all_trims(), "default_size": config.get_label_size()}

    def page() -> bytes:
        return resources.files("ble_thermal_printer").joinpath("web.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        timeout = SOCKET_TIMEOUT

        def version_string(self) -> str:
            return "ble-thermal-printer"  # don't advertise the Python version

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in {**httpguard.SECURITY_HEADERS, **(headers or {})}.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _reject(self, r: httpguard.Rejected) -> None:
            self.close_connection = True  # the unread body (if any) must not be parsed as a request
            self._json({"ok": False, "error": {"code": r.code, "params": {}, "text": str(r)}}, r.status)

        def _body(self):
            n = httpguard.body_length(self.headers)
            raw = self.rfile.read(n) if n else b""
            if len(raw) != n:
                raise httpguard.Rejected(400, "bad_request", "Incomplete request body")
            try:
                return json.loads(raw or b"{}")
            except (UnicodeDecodeError, ValueError):
                raise httpguard.Rejected(400, "bad_json", "Request body is not valid JSON") from None

        def do_GET(self):
            try:
                httpguard.check("GET", self.headers, hosts)
            except httpguard.Rejected as r:
                return self._reject(r)
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, page(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(full_state())
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                httpguard.check("POST", self.headers, hosts)
                req = self._body()
            except httpguard.Rejected as r:
                log.warning("Rejected request (%s): %s", path, r)
                return self._reject(r)
            try:
                if path == "/api/connect":
                    mgr.run(mgr.connect(schema.connect_address(req)), 60)
                elif path == "/api/disconnect":
                    mgr.run(mgr.disconnect(), 15)
                elif path == "/api/scan":
                    timeout = schema.scan_timeout(req)
                    mgr.run(mgr.scan(timeout), timeout + 25)
                elif path == "/api/refresh":
                    mgr.run(mgr.refresh(), 15)
                elif path == "/api/settings":
                    on = schema.auto_reconnect(req)
                    if on is not None:
                        mgr.set_auto_reconnect(on)
                elif path == "/api/trim":
                    config.set_trim(*schema.trim_request(req))
                elif path in ("/api/preview", "/api/print"):
                    img, label, lr = compose_from_json(req)
                    if path == "/api/preview":
                        dpm = label.dots_per_mm
                        if lr.kind == "calibrate":
                            m, (top, bottom) = 0.0, (0.0, 0.0)
                        else:
                            m = render.effective_margin_mm(lr.margin_mm, lr.border)
                            top, bottom = config.get_trim(label.width_mm, label.height_mm)
                        return self._send(200, render.to_png_bytes(img), "image/png", {
                            "X-Margin-Px": str(round(m * dpm)),
                            "X-Trim-Px": f"{round(top * dpm)},{round(bottom * dpm)}",
                        })
                    mgr.run(mgr.print(img, label.width_mm, label.height_mm, lr.density, lr.copies))
                else:
                    return self._send(404, b"not found", "text/plain")
                self._json({"ok": True, "state": full_state()})
            except Exception as e:
                log.warning("Request error (%s): %s", path, e)
                self._json({"ok": False, "error": err_info(e), "state": full_state()}, 400)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, target: str | None = None, open_browser=True,
          autoconnect: bool = True, allowed_hosts: list[str] | tuple[str, ...] = ()) -> None:
    logging.getLogger("ble_thermal_printer").setLevel(logging.INFO)
    cfg_hosts = config.load().get("allowed_hosts") or []
    if isinstance(cfg_hosts, str):
        cfg_hosts = [cfg_hosts]
    hosts = httpguard.allowed_hosts(host, [*map(str, cfg_hosts), *allowed_hosts])
    mgr = ConnectionManager(target, autoconnect=autoconnect)
    httpd = ThreadingHTTPServer((host, port), make_handler(mgr, hosts))
    url = f"http://{'127.0.0.1' if host in httpguard.WILDCARD else host}:{port}/"
    print(t("serve_url", url=url), flush=True)
    if not httpguard.is_loopback(host):
        print(t("serve_exposed"), flush=True)
    log.info("Allowed Host names: %s", "any" if hosts is None else ", ".join(sorted(hosts)))
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            mgr.run(mgr.shutdown(), 5)
        except Exception:
            pass
        httpd.server_close()
