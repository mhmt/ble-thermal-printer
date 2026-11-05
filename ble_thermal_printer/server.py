"""Yerel web arayüzü: http://127.0.0.1:8765 — bağlantı yönetimi manager.py içinde.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from PIL import Image

from . import config, render
from .i18n import t
from .manager import ConnectionManager, err_info

log = logging.getLogger("ble_thermal_printer.server")


def compose_from_json(req: dict) -> tuple[Image.Image, render.Label]:
    image = None
    if req.get("kind") == "image":
        data_url = req.get("image") or ""
        if "," not in data_url:
            raise render.RenderError("no_image", "No image selected")
        image = Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))
    font_size = int(req["font_size"]) if req.get("font_size") else None
    w, h = float(req.get("width_mm", 50)), float(req.get("height_mm", 16))
    if req.get("kind") == "calibrate":
        return render.render_calibration(w, h)
    top, bottom = config.get_trim(w, h)
    return render.compose(
        req.get("kind", "text"), trim_top_mm=top, trim_bottom_mm=bottom,
        width_mm=w, height_mm=h,
        margin_mm=float(req.get("margin_mm", 1.5)), rotate=int(req.get("rotate", 0)),
        border=bool(req.get("border")), invert=bool(req.get("invert")),
        text=req.get("text", ""), font_style=req.get("font_style", "bold"), font_size=font_size,
        align=req.get("align", "center"), data=req.get("data", ""), caption=req.get("caption"),
        caption_side=req.get("caption_side", "right"), barcode_kind=req.get("barcode_kind", "code128"),
        show_text=bool(req.get("show_text", True)), image=image, dither=bool(req.get("dither", True)),
    )


def make_handler(mgr: ConnectionManager):
    def full_state() -> dict:
        return {**mgr.snapshot(), "trims": config.all_trims()}

    def page() -> bytes:
        return resources.files("ble_thermal_printer").joinpath("web.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, page(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._json(full_state())
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            try:
                req = self._body()
                if self.path == "/api/connect":
                    mgr.run(mgr.connect(req.get("address")), 60)
                elif self.path == "/api/disconnect":
                    mgr.run(mgr.disconnect(), 15)
                elif self.path == "/api/scan":
                    mgr.run(mgr.scan(float(req.get("timeout", 5))), 30)
                elif self.path == "/api/refresh":
                    mgr.run(mgr.refresh(), 15)
                elif self.path == "/api/settings":
                    if "auto_reconnect" in req:
                        mgr.set_auto_reconnect(bool(req["auto_reconnect"]))
                elif self.path == "/api/trim":
                    config.set_trim(float(req["width_mm"]), float(req["height_mm"]),
                                    float(req.get("top_mm") or 0), float(req.get("bottom_mm") or 0))
                elif self.path in ("/api/preview", "/api/print"):
                    img, label = compose_from_json(req)
                    if self.path == "/api/preview":
                        dpm = label.dots_per_mm
                        if req.get("kind") == "calibrate":
                            m, (top, bottom) = 0.0, (0.0, 0.0)
                        else:
                            m = render.effective_margin_mm(float(req.get("margin_mm", 1.5)), bool(req.get("border")))
                            top, bottom = config.get_trim(label.width_mm, label.height_mm)
                        return self._send(200, render.to_png_bytes(img), "image/png", {
                            "X-Margin-Px": str(round(m * dpm)),
                            "X-Trim-Px": f"{round(top * dpm)},{round(bottom * dpm)}",
                        })
                    density = int(req["density"]) if req.get("density") else None
                    copies = max(1, min(99, int(req.get("copies", 1))))
                    mgr.run(mgr.print(img, label.width_mm, label.height_mm, density, copies))
                else:
                    return self._send(404, b"not found", "text/plain")
                self._json({"ok": True, "state": full_state()})
            except Exception as e:
                log.warning("Request error (%s): %s", self.path, e)
                self._json({"ok": False, "error": err_info(e), "state": full_state()}, 400)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, target: str | None = None, open_browser=True,
          autoconnect: bool = True) -> None:
    logging.getLogger("ble_thermal_printer").setLevel(logging.INFO)
    mgr = ConnectionManager(target, autoconnect=autoconnect)
    httpd = ThreadingHTTPServer((host, port), make_handler(mgr))
    url = f"http://{host}:{port}/"
    print(t("serve_url", url=url), flush=True)
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
