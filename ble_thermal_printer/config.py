"""Persistent settings in ~/.config/ble-thermal-printer/config.json (shared by CLI and web server)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("ble_thermal_printer.config")

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ble-thermal-printer" / "config.json"

# Measured on the 50x16 mm roll: the last ~0.6 mm of a 16 mm image falls into the gap.
DEFAULT_TRIMS = {"50x16": {"top_mm": 0.0, "bottom_mm": 0.6}}


def load() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    except Exception as e:
        log.warning("Could not save config: %s", e)


def size_key(width_mm: float, height_mm: float) -> str:
    return f"{float(width_mm):g}x{float(height_mm):g}"


def get_trim(width_mm: float, height_mm: float, cfg: dict | None = None) -> tuple[float, float]:
    """Unprintable (top, bottom) band in mm for this label size."""
    cfg = load() if cfg is None else cfg
    key = size_key(width_mm, height_mm)
    t = (cfg.get("trims") or {}).get(key) or DEFAULT_TRIMS.get(key) or {}
    return float(t.get("top_mm", 0)), float(t.get("bottom_mm", 0))


def all_trims(cfg: dict | None = None) -> dict:
    cfg = load() if cfg is None else cfg
    return {**DEFAULT_TRIMS, **(cfg.get("trims") or {})}


def set_trim(width_mm: float, height_mm: float, top_mm: float, bottom_mm: float) -> None:
    cfg = load()
    cfg.setdefault("trims", {})[size_key(width_mm, height_mm)] = {
        "top_mm": max(0.0, float(top_mm)), "bottom_mm": max(0.0, float(bottom_mm)),
    }
    save(cfg)
