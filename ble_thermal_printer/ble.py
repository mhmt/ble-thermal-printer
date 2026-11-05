"""BLE bağlantısı (bleak) — macOS / Linux / Windows."""
from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from .protocol import (
    DEVICE_INFO_LENGTH,
    DEVICE_INFO_QUERY,
    NOTIFY_UUID,
    SERVICE_UUID,
    STATUS_LENGTH,
    STATUS_QUERY,
    WRITE_UUID,
    DeviceInfo,
    Status,
)

log = logging.getLogger("ble_thermal_printer")


class PrinterError(RuntimeError):
    """Coded printer error: str() is English; code/params let the CLI and web UI translate it."""

    def __init__(self, code: str, message: str, **params):
        super().__init__(message)
        self.code = code
        self.params = params


# Advertised name prefixes of known compatible printers (MPIA P1 advertises as "P1_xxxx").
NAME_PREFIXES = ("P1_",)


def _looks_like_printer(dev: BLEDevice, adv) -> bool:
    name = dev.name or adv.local_name or ""
    uuids = [u.lower() for u in (adv.service_uuids or [])]
    return name.startswith(NAME_PREFIXES) or SERVICE_UUID in uuids


async def scan(timeout: float = 6.0) -> list[tuple[BLEDevice, int]]:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = [(d, a.rssi) for d, a in found.values() if _looks_like_printer(d, a)]
    return sorted(out, key=lambda t: -t[1])


async def find(target: str | None = None, timeout: float = 8.0) -> BLEDevice:
    """target: BLE adı/alt dizgesi ya da adres. Boşsa ilk bulunan uyumlu yazıcı."""
    def match(d: BLEDevice, a) -> bool:
        if target:
            t = target.lower()
            return d.address.lower() == t or t in (d.name or a.local_name or "").lower()
        return _looks_like_printer(d, a)

    dev = await BleakScanner.find_device_by_filter(match, timeout=timeout)
    if dev is None:
        raise PrinterError(
            "not_found", "Printer not found. Is it on? If the phone app is connected, disconnect it first."
        )
    return dev


class Printer:
    """async with Printer() as p: await p.print_job(...)"""

    def __init__(self, target: str | None = None, *, chunk: int | None = None, delay: float = 0.0,
                 on_disconnect=None, scan_timeout: float = 8.0):
        self.target = target
        self.chunk = chunk
        self.delay = delay
        self.on_disconnect = on_disconnect
        self.scan_timeout = scan_timeout
        self.client: BleakClient | None = None
        self.device: BLEDevice | None = None
        self._rx = bytearray()
        self._rx_event = asyncio.Event()
        self.info: DeviceInfo | None = None

    async def __aenter__(self) -> "Printer":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        dev = self.device = await find(self.target, self.scan_timeout)
        log.info("Connecting: %s (%s)", dev.name, dev.address)
        cb = (lambda _c: self.on_disconnect(self)) if self.on_disconnect else None
        self.client = BleakClient(dev, disconnected_callback=cb, timeout=15.0)
        await self.client.connect()
        await self.client.start_notify(NOTIFY_UUID, self._on_notify)
        if self.chunk is None:
            self.chunk = max(20, min(self.client.mtu_size - 3, 237))
        log.info("Connected. MTU=%s chunk=%s", self.client.mtu_size, self.chunk)

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(NOTIFY_UUID)
            except Exception:
                pass
            await self.client.disconnect()

    @property
    def name(self) -> str:
        return (self.device and self.device.name) or getattr(self.client, "name", None) or "?"

    @property
    def address(self) -> str | None:
        return self.device.address if self.device else None

    @property
    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    def _on_notify(self, _char, data: bytearray) -> None:
        log.debug("<< %s", bytes(data).hex(" "))
        self._rx.extend(data)
        self._rx_event.set()

    async def _query(self, packet: bytes, length: int, timeout: float = 3.0) -> bytes:
        self._rx.clear()
        await self._write(packet)
        deadline = time.monotonic() + timeout
        while len(self._rx) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"No reply ({len(self._rx)}/{length} bytes)")
            self._rx_event.clear()
            try:
                await asyncio.wait_for(self._rx_event.wait(), remaining)
            except asyncio.TimeoutError:
                pass
        data = bytes(self._rx)
        self._rx.clear()
        return data

    async def _write(self, data: bytes, progress=None) -> None:
        assert self.client is not None and self.chunk
        total = len(data)
        for i in range(0, total, self.chunk):
            await self.client.write_gatt_char(WRITE_UUID, data[i : i + self.chunk], response=False)
            if self.delay:
                await asyncio.sleep(self.delay)
            if progress:
                progress(min(i + self.chunk, total), total)

    async def get_info(self) -> DeviceInfo:
        self.info = DeviceInfo.parse(await self._query(DEVICE_INFO_QUERY, DEVICE_INFO_LENGTH))
        return self.info

    async def get_status(self) -> Status:
        data = await self._query(STATUS_QUERY, STATUS_LENGTH)
        if data.startswith(b"err:"):
            data = data[4:]
        return Status(data[:STATUS_LENGTH])

    async def send_job(self, job: bytes, progress=None, settle: float = 1.0) -> None:
        self._rx.clear()
        await self._write(job, progress)
        # Son paketlerin yazıcıya ulaşması için kısa bekleme
        await asyncio.sleep(settle)
        if self._rx:
            log.info("Printer message after job: %s", bytes(self._rx).hex(" "))

    async def print_image(self, img, label_w_mm: float, label_h_mm: float, *,
                          density: int | None = None, copies: int = 1, progress=None) -> None:
        """Durumu kontrol eder, TSPL işi üretir ve gönderir."""
        from .protocol import build_tspl_job

        info = self.info or await self.get_info()
        st = await self.get_status()
        if not st.ok:
            raise PrinterError("not_ready", f"Printer not ready: {', '.join(st.errors)}", errors=st.codes)
        if info.command_mode != 2:
            raise PrinterError("esc_unsupported", "The device is in ESC mode; only TSPL is supported for now.")
        if img.width > info.width:
            raise PrinterError("too_wide", f"Image is {img.width} dots wide, the printer prints at most {info.width} dots.",
                               width=img.width, max=info.width)
        job = build_tspl_job(img, label_w_mm=label_w_mm, label_h_mm=label_h_mm,
                             dots_per_mm=info.dots_per_mm, density=density,
                             copies=copies, compress=info.compression)
        await self.send_job(job, progress)
