"""Persistent BLE connection manager (used by the web interface).

Phases:
  idle        not connected; the user disconnected or never connected
  connecting  scanning and connecting
  connected   connected (status.errors is non-empty if the printer reports a problem)
  printing    a job is being sent
  lost        the connection dropped unexpectedly; waiting for auto-reconnect
  error       a connection attempt failed; an automatic retry may be scheduled

All BLE work runs on one asyncio loop (in its own thread) under a single lock;
HTTP threads only read snapshot() or submit work through run().
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque

from bleak import BleakScanner

from . import config
from .ble import Printer, PrinterError, _looks_like_printer
from .render import RenderError

log = logging.getLogger("ble_thermal_printer.manager")

STATUS_INTERVAL = 5.0       # status poll interval while connected (s)
BACKOFF = (3, 5, 10, 20, 30)  # reconnect backoff delays (s)


def err_info(e: BaseException) -> dict:
    """Error -> {code, params, text}. The UI translates code+params; text is the raw fallback."""
    text = str(e) or e.__class__.__name__
    if isinstance(e, (PrinterError, RenderError)):
        return {"code": e.code, "params": e.params, "text": text}
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return {"code": "timeout", "params": {}, "text": text}
    if "not found" in text.lower():
        return {"code": "not_found", "params": {}, "text": text}
    return {"code": "other", "params": {}, "text": text}


class ConnectionManager:
    def __init__(self, target: str | None = None, *, autoconnect: bool = True):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="ble-loop").start()
        self._lock = threading.Lock()          # guards the fields read by snapshot()
        self.cfg = self._load_cfg()

        self.target = target or self.cfg.get("last_address")
        self.printer: Printer | None = None
        self.phase = "idle"
        self.error: str | None = None
        self.device: dict | None = (
            {"name": self.cfg.get("last_name"), "address": self.cfg["last_address"]}
            if self.cfg.get("last_address") else None
        )
        self.info: dict | None = None
        self.status: dict | None = None
        self.connected_at: float | None = None
        self.status_at: float | None = None
        self.auto_reconnect: bool = self.cfg.get("auto_reconnect", True)
        self.want_connected = autoconnect
        self.retry_count = 0
        self.next_retry: float | None = time.time() if autoconnect else None
        self.scanning = False
        self.devices: dict[str, dict] = {}
        self.events: deque[dict] = deque(maxlen=40)
        self.progress = {"done": 0, "total": 0}
        self._intentional = False

        self.run(self._init_async(), timeout=5)
        asyncio.run_coroutine_threadsafe(self._monitor(), self.loop)
        self._event("server_started", device=(self.device or {}).get("name"))

    async def _init_async(self) -> None:
        self.op = asyncio.Lock()

    # --- helpers -------------------------------------------------------------
    def run(self, coro, timeout: float = 120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _load_cfg(self) -> dict:
        return config.load()

    def _save_cfg(self) -> None:
        # Merge only the keys this manager owns; trims etc. may have changed on disk.
        cfg = config.load()
        for k in ("last_address", "last_name", "auto_reconnect"):
            if k in self.cfg:
                cfg[k] = self.cfg[k]
        config.save(cfg)

    def _event(self, code: str, level: str = "info", **params) -> None:
        log.info("%s %s", code, params or "")
        with self._lock:
            self.events.appendleft({"t": time.time(), "code": code, "params": params, "level": level})

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            retry_in = None
            if self.next_retry and self.phase in ("lost", "error", "idle") and self.want_connected and self.auto_reconnect:
                retry_in = max(0, round(self.next_retry - now))
            return {
                "phase": self.phase, "error": self.error, "device": self.device, "info": self.info,
                "status": self.status, "connected_at": self.connected_at, "status_at": self.status_at,
                "auto_reconnect": self.auto_reconnect, "want_connected": self.want_connected,
                "retry_in": retry_in, "retry_count": self.retry_count, "scanning": self.scanning,
                "devices": sorted(self.devices.values(), key=lambda d: -(d.get("rssi") or -999)),
                "events": list(self.events)[:15], "progress": self.progress, "now": now,
            }

    # --- connection ----------------------------------------------------------
    def _on_lost(self, printer: Printer) -> None:
        # bleak invokes this callback on the event loop thread
        if printer is not self.printer or self._intentional:
            return
        self._set(printer=None, phase="lost", connected_at=None,
                  error={"code": "lost", "params": {}, "text": "connection lost"})
        self._schedule_retry(first=True)
        self._event("lost", "warn")

    def _schedule_retry(self, first: bool = False) -> None:
        if not (self.want_connected and self.auto_reconnect):
            self._set(next_retry=None)
            return
        if first:
            self.retry_count = 0
        delay = BACKOFF[min(self.retry_count, len(BACKOFF) - 1)]
        self._set(next_retry=time.time() + delay)

    async def _connect_locked(self, address: str | None, *, manual: bool) -> None:
        if self.printer and self.printer.is_connected:
            if not address or address == self.printer.address:
                return
            await self._disconnect_locked(reason="switching")
        target = address or self.target
        self._set(phase="connecting", error=None, next_retry=None)
        self._event("connecting", target=self._label(target), attempt=None if manual else self.retry_count + 1)
        p = Printer(target, on_disconnect=self._on_lost, scan_timeout=8.0)
        try:
            await p.connect()
            info = await p.get_info()
            st = await p.get_status()
        except Exception as e:
            try:
                await p.disconnect()
            except Exception:
                pass
            self.retry_count += 0 if manual else 1
            self._set(phase="error", error=err_info(e))
            self._event("connect_failed", "error", error=err_info(e))
            self._schedule_retry()
            raise
        self._intentional = False
        dev = {"name": p.name, "address": p.address,
               "rssi": self.devices.get(p.address, {}).get("rssi")}
        self._set(
            printer=p, phase="connected", error=None, device=dev, target=p.address,
            connected_at=time.time(), retry_count=0, next_retry=None, want_connected=True,
            info={"firmware": info.version, "width": info.width, "width_mm": info.width / info.dots_per_mm,
                  "dpi": info.dpi, "mode": info.mode_name, "density": info.density, "speed": info.speed,
                  "paper_type": info.paper_type, "compression": info.compression, "mtu": p.client.mtu_size},
        )
        self._apply_status(st)
        self.cfg.update(last_address=p.address, last_name=p.name)
        self._save_cfg()
        self._event("connected", name=p.name)

    def _label(self, target: str | None) -> str | None:
        if not target:
            return None  # the UI shows "first compatible printer found"
        known = self.devices.get(target) or (self.device if self.device and self.device.get("address") == target else None)
        return (known or {}).get("name") or target

    def _apply_status(self, st) -> None:
        prev = (self.status or {}).get("errors")
        self._set(status={"raw": st.raw.hex(" "), "errors": st.codes, "ready": st.ok}, status_at=time.time())
        if prev is not None and prev != st.codes:
            if st.codes:
                self._event("printer_warning", "warn", errors=st.codes)
            else:
                self._event("printer_ready")

    async def _disconnect_locked(self, reason: str = "disconnected") -> None:
        p = self.printer
        self._intentional = True
        self._set(printer=None, phase="idle", connected_at=None, error=None, next_retry=None)
        if p:
            try:
                await p.disconnect()
            except Exception:
                pass
            self._event(reason)

    async def connect(self, address: str | None = None) -> None:
        async with self.op:
            self._set(want_connected=True)
            await self._connect_locked(address, manual=True)

    async def disconnect(self) -> None:
        async with self.op:
            self._set(want_connected=False)
            await self._disconnect_locked()

    async def refresh(self) -> None:
        async with self.op:
            if self.printer and self.printer.is_connected:
                self._apply_status(await self.printer.get_status())

    def set_auto_reconnect(self, on: bool) -> None:
        self._set(auto_reconnect=on)
        self.cfg["auto_reconnect"] = on
        self._save_cfg()
        if on and self.want_connected and self.phase in ("lost", "error"):
            self._set(next_retry=time.time())
        self._event("auto_reconnect", on=on)

    async def scan(self, timeout: float = 5.0) -> list[dict]:
        if self.scanning:
            return self.snapshot()["devices"]
        self._set(scanning=True)
        try:
            found = await BleakScanner.discover(timeout=timeout, return_adv=True)
            now = time.time()
            devs = {}
            for d, a in found.values():
                if _looks_like_printer(d, a):
                    devs[d.address] = {"name": d.name or a.local_name or "?", "address": d.address,
                                       "rssi": a.rssi, "seen": now}
            # a connected device stops advertising; keep it in the list
            if self.printer and self.printer.is_connected and self.printer.address not in devs:
                devs[self.printer.address] = {**(self.device or {}), "seen": now}
            self._set(devices=devs)
            self._event("scan_done", count=len(devs))
        finally:
            self._set(scanning=False)
        return self.snapshot()["devices"]

    # --- printing ------------------------------------------------------------
    async def print(self, img, w_mm: float, h_mm: float, density, copies, *,
                    connect: bool = True, source: str | None = None) -> None:
        """connect=False fails fast with `not_connected` instead of connecting first (API calls)."""
        async with self.op:
            if not (self.printer and self.printer.is_connected):
                if not connect:
                    raise PrinterError("not_connected", "Printer is not connected")
                self._set(want_connected=True)
                await self._connect_locked(None, manual=True)
            p = self.printer

            def prog(done, total):
                self.progress = {"done": done, "total": total}

            self._set(phase="printing", progress={"done": 0, "total": 0})
            try:
                await p.print_image(img, w_mm, h_mm, density=density, copies=copies, progress=prog)
                self._event("printed", copies=copies, w=w_mm, h=h_mm, source=source)
            except Exception as e:
                self._event("print_failed", "error", error=err_info(e))
                raise
            finally:
                if self.phase == "printing":
                    self._set(phase="connected" if p.is_connected else "lost")
            try:
                self._apply_status(await p.get_status())
            except Exception:
                pass

    # --- background monitor --------------------------------------------------
    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                now = time.time()
                if self.phase == "connected" and not self.op.locked() and \
                        (self.status_at is None or now - self.status_at >= STATUS_INTERVAL):
                    async with self.op:
                        if self.printer and self.printer.is_connected:
                            try:
                                self._apply_status(await self.printer.get_status())
                            except Exception as e:
                                log.warning("Status query failed: %s", e)
                                if not self.printer.is_connected:
                                    self._on_lost(self.printer)
                elif (self.phase in ("lost", "error", "idle") and self.want_connected and self.auto_reconnect
                      and self.next_retry and now >= self.next_retry and not self.op.locked()):
                    async with self.op:
                        try:
                            await self._connect_locked(None, manual=False)
                        except Exception:
                            pass
            except Exception:
                log.exception("Monitor error")

    async def shutdown(self) -> None:
        async with self.op:
            await self._disconnect_locked("shutdown")
