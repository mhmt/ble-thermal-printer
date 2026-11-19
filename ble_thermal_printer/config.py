"""Persistent settings in ~/.config/ble-thermal-printer/config.json (shared by CLI and web server)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("ble_thermal_printer.config")

# BLE_THERMAL_PRINTER_CONFIG points to the file directly (e.g. /var/lib/... for a system service).
CONFIG_PATH = Path(
    os.environ.get("BLE_THERMAL_PRINTER_CONFIG")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ble-thermal-printer" / "config.json"
)

DEFAULT_LABEL_SIZE = (40.0, 30.0)


def load() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save(cfg: dict) -> None:
    """Write atomically (readers never see a half-written file), readable by the owner only:
    the file holds API token hashes."""
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(cfg, indent=2, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        log.warning("Could not save config: %s", e)
        tmp.unlink(missing_ok=True)


def get_label_size(cfg: dict | None = None) -> tuple[float, float]:
    """Default label size: `label_size` ("WxH" in mm) from the config, else 40x30."""
    cfg = load() if cfg is None else cfg
    try:
        w, h = str(cfg["label_size"]).lower().split("x")
        return float(w), float(h)
    except (KeyError, ValueError):
        return DEFAULT_LABEL_SIZE


def size_key(width_mm: float, height_mm: float) -> str:
    return f"{float(width_mm):g}x{float(height_mm):g}"


def get_trim(width_mm: float, height_mm: float, cfg: dict | None = None) -> tuple[float, float]:
    """Unprintable (top, bottom) band in mm for this label size."""
    cfg = load() if cfg is None else cfg
    key = size_key(width_mm, height_mm)
    t = (cfg.get("trims") or {}).get(key) or {}
    return float(t.get("top_mm", 0)), float(t.get("bottom_mm", 0))


def all_trims(cfg: dict | None = None) -> dict:
    cfg = load() if cfg is None else cfg
    return dict(cfg.get("trims") or {})


def set_trim(width_mm: float, height_mm: float, top_mm: float, bottom_mm: float) -> None:
    cfg = load()
    cfg.setdefault("trims", {})[size_key(width_mm, height_mm)] = {
        "top_mm": max(0.0, float(top_mm)), "bottom_mm": max(0.0, float(bottom_mm)),
    }
    save(cfg)
