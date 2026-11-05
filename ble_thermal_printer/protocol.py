"""Eleph-label (JX / com.sandu.JxPrinter) protokolü: sorgular, durum ve TSPL iş üretimi.

Doğrulanmış cihaz: MPIA P1 (BLE adı P1_xxxx), 203 DPI, 384 nokta baskı genişliği.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass

from PIL import Image

# --- BLE GATT --------------------------------------------------------------
SERVICE_UUID = "000018f0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "00002af1-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "00002af0-0000-1000-8000-00805f9b34fb"

# --- Sorgular --------------------------------------------------------------
DEVICE_INFO_QUERY = b"\x1b##JXIG"   # 116 bayt cevap
DEVICE_INFO_LENGTH = 116
STATUS_QUERY = b"\x1d\x61\x00"       # 4 bayt cevap (ya da "err:" + 4 bayt olay)
STATUS_LENGTH = 4

DOTS_PER_MM = {0: 8, 1: 12, 2: 24}


@dataclass(frozen=True)
class DeviceInfo:
    raw: bytes
    version: int
    width: int          # basılabilir genişlik (nokta)
    dpi_type: int
    command_mode: int   # 1 = ESC, 2 = TSPL
    speed: int
    density: int
    paper_type: int     # 1 = boşluklu etiket (gözlemlenen)
    compression: bool

    @classmethod
    def parse(cls, data: bytes) -> "DeviceInfo":
        if len(data) < DEVICE_INFO_LENGTH:
            raise ValueError(f"Device info must be 116 bytes, got {len(data)}")
        return cls(
            raw=bytes(data[:DEVICE_INFO_LENGTH]),
            version=int.from_bytes(data[6:8], "little"),
            width=int.from_bytes(data[8:10], "little"),
            dpi_type=data[10],
            command_mode=data[11],
            speed=data[14],
            density=data[15],
            paper_type=data[17],
            compression=data[22] == 1,
        )

    @property
    def dots_per_mm(self) -> int:
        return DOTS_PER_MM.get(self.dpi_type, 8)

    @property
    def dpi(self) -> int:
        return round(self.dots_per_mm * 25.4)

    @property
    def mode_name(self) -> str:
        return {1: "ESC", 2: "TSPL"}.get(self.command_mode, f"?{self.command_mode}")


@dataclass(frozen=True)
class Status:
    raw: bytes

    @property
    def errors(self) -> list[str]:
        b0, b1, b2, _ = self.raw
        errs = []
        if b0 & 0x20:
            errs.append("cover open")
        if b2 & 0x0C == 0x0C:
            errs.append("out of paper")
        if b1 & 0x40:
            errs.append("overheated")
        if b1 & 0x08:
            errs.append("cutter error")
        return errs

    @property
    def codes(self) -> list[str]:
        """Dile bağımsız hata kodları (web arayüzü çevirir)."""
        b0, b1, b2, _ = self.raw
        return [c for c, on in (("cover_open", b0 & 0x20), ("paper_out", b2 & 0x0C == 0x0C),
                                ("overheated", b1 & 0x40), ("cutter_error", b1 & 0x08)) if on]

    @property
    def ok(self) -> bool:
        return not self.errors

    def __str__(self) -> str:
        return f"{self.raw.hex(' ')} ({', '.join(self.errors) or 'ready'})"


# --- Raster ----------------------------------------------------------------
def pack_rows(img: Image.Image) -> tuple[int, int, bytes]:
    """1-bit görüntüyü MSB-first satırlara paketler; 1 = siyah."""
    img = img.convert("1")
    w, h = img.size
    stride = (w + 7) // 8
    # PIL "1" modunda 0=siyah, 255=beyaz; tobytes() MSB-first ve 1=beyaz paketler.
    data = img.tobytes()
    # 1 = siyah olacak şekilde çevir; satır sonu dolgu bitleri beyaz (0) kalsın.
    out = bytearray(b ^ 0xFF for b in data)
    pad = stride * 8 - w
    if pad:
        mask = (0xFF << pad) & 0xFF
        for r in range(h):
            out[r * stride + stride - 1] &= mask
    return stride, h, bytes(out)


# --- TSPL iş üretimi ------------------------------------------------------
def build_tspl_job(
    img: Image.Image,
    *,
    label_w_mm: float,
    label_h_mm: float,
    dots_per_mm: int = 8,
    density: int | None = None,
    speed: int | None = None,
    copies: int = 1,
    compress: bool = False,
    x: int = 0,
    y: int = 0,
) -> bytes:
    stride, h, black_one = pack_rows(img)

    def cmd(s: str) -> bytes:
        return s.encode("ascii") + b"\n"

    job = bytearray()
    job += cmd(f"SIZE {_fmt(label_w_mm)} mm,{_fmt(label_h_mm)} mm")
    if speed is not None:
        job += cmd(f"SPEED {speed:02x}")
    if density is not None:
        job += cmd(f"DENSITY {density}")
    job += cmd("CLS")
    job += cmd("DIRECTION 0")
    if compress:
        comp = zlib.compress(black_one, 6)
        job += f"BITMAP {x},{y},{stride},{h},3,{len(comp)},".encode() + comp + b"\n"
    else:
        # TSPL mod 0: 0 = siyah nokta → bitleri ters çevir
        inv = bytes(b ^ 0xFF for b in black_one)
        job += f"BITMAP {x},{y},{stride},{h},0,".encode() + inv + b"\n"
    job += cmd(f"PRINT 1,{copies}")
    return bytes(job)


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"
