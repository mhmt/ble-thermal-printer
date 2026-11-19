"""Validation of label requests coming over HTTP (web UI now, webhook API later).

Everything that reaches render.compose() from the network goes through label_request(), so
types, ranges and sizes are checked in one place before any rendering or printing happens.
Errors are RenderError subclasses with a code, so the UI can translate them.
"""
from __future__ import annotations

import base64
import binascii
import io
import math
import warnings
from dataclasses import dataclass, field

from PIL import Image

from . import config
from .render import RenderError

# Ranges match the web UI inputs; the print head is 48 mm wide, labels can be long.
WIDTH_MM = (10.0, 60.0)
HEIGHT_MM = (5.0, 200.0)
MARGIN_MM = (0.0, 10.0)
TRIM_MM = (0.0, 10.0)
FONT_SIZE = (6, 300)
DENSITY = (1, 15)
COPIES = (1, 99)
SCAN_TIMEOUT = (1.0, 30.0)
MAX_TEXT = 1000          # characters
MAX_DATA = 1000          # QR / barcode payload characters
MAX_CAPTION = 200
MAX_ADDRESS = 64
MAX_IMAGE_PIXELS = 30_000_000   # decoded size; a 12 MP phone photo is well below this
IMAGE_FORMATS = ("PNG", "JPEG", "GIF", "BMP", "WEBP")

KINDS = ("text", "qr", "barcode", "image", "calibrate")
FONT_STYLES = ("bold", "regular", "mono")
ALIGNS = ("center", "left", "right")
CAPTION_SIDES = ("right", "bottom")
ROTATIONS = (0, 90, 180, 270)


def _barcode_kinds() -> tuple[str, ...]:
    import barcode
    return tuple(barcode.PROVIDED_BARCODES)


class ValidationError(RenderError):
    """A request field is missing, has the wrong type or is out of range."""

    def __init__(self, field_name: str, reason: str, **params):
        super().__init__("invalid", f"Invalid value for '{field_name}': {reason}",
                         field=field_name, reason=reason, **params)


# --- field readers -----------------------------------------------------------
def _number(req: dict, key: str, lo: float, hi: float, default: float | None = None) -> float | None:
    v = req.get(key)
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise ValidationError(key, "not a number")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValidationError(key, "not a number") from None
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValidationError(key, f"must be between {lo:g} and {hi:g}", min=lo, max=hi)
    return x


def _int(req: dict, key: str, lo: int, hi: int, default: int | None = None) -> int | None:
    x = _number(req, key, lo, hi, None)
    if x is None:
        return default
    if x != int(x):
        raise ValidationError(key, "must be a whole number")
    return int(x)


def _bool(req: dict, key: str, default: bool) -> bool:
    v = req.get(key, default)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    raise ValidationError(key, "must be true or false")


def _str(req: dict, key: str, max_len: int, default: str = "") -> str:
    v = req.get(key)
    if v is None:
        return default
    if not isinstance(v, str):
        raise ValidationError(key, "must be a string")
    if len(v) > max_len:
        raise ValidationError(key, f"longer than {max_len} characters", max=max_len)
    return v


def _choice(req: dict, key: str, choices: tuple, default):
    v = req.get(key)
    if v is None or v == "":
        return default
    if v not in choices:
        raise ValidationError(key, "must be one of " + ", ".join(map(str, choices)))
    return v


def decode_image(data_url) -> Image.Image:
    """`data:image/...;base64,<data>` -> decoded PIL image, with format and pixel limits."""
    if not isinstance(data_url, str) or "," not in data_url:
        raise RenderError("no_image", "No image selected")
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("image", "not valid base64") from None
    too_large = ValidationError("image", "image is too large", max_pixels=MAX_IMAGE_PIXELS)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(raw), formats=IMAGE_FORMATS)
            w, h = img.size  # read from the header, before decoding the pixels
            if w * h > MAX_IMAGE_PIXELS:
                raise too_large
            img.load()
    except ValidationError:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise too_large from None
    except Exception:
        raise ValidationError("image", "not a supported image (PNG, JPEG, GIF, BMP, WebP)") from None
    return img


# --- requests ----------------------------------------------------------------
@dataclass
class LabelRequest:
    kind: str
    width_mm: float
    height_mm: float
    compose_kwargs: dict = field(default_factory=dict)  # for render.compose (kind != calibrate)
    margin_mm: float = 1.5
    border: bool = False
    density: int | None = None
    copies: int = 1


def _require_dict(req) -> dict:
    if not isinstance(req, dict):
        raise ValidationError("body", "must be a JSON object")
    return req


def label_request(req, *, max_copies: int = COPIES[1]) -> LabelRequest:
    req = _require_dict(req)
    kind = _choice(req, "kind", KINDS, "text")
    dw, dh = config.get_label_size()
    w = _number(req, "width_mm", *WIDTH_MM, default=dw)
    h = _number(req, "height_mm", *HEIGHT_MM, default=dh)
    if not (WIDTH_MM[0] <= w <= WIDTH_MM[1] and HEIGHT_MM[0] <= h <= HEIGHT_MM[1]):
        raise ValidationError("label_size", "configured default label size is out of range")
    out = LabelRequest(
        kind=kind, width_mm=w, height_mm=h,
        density=_int(req, "density", *DENSITY),
        copies=_int(req, "copies", COPIES[0], max_copies, default=1),
    )
    if kind == "calibrate":
        return out
    out.margin_mm = _number(req, "margin_mm", *MARGIN_MM, default=1.5)
    out.border = _bool(req, "border", False)
    kw = dict(
        margin_mm=out.margin_mm, border=out.border,
        rotate=int(_choice(req, "rotate", ROTATIONS, 0)),
        invert=_bool(req, "invert", False),
    )
    if kind == "text":
        kw.update(text=_str(req, "text", MAX_TEXT),
                  font_style=_choice(req, "font_style", FONT_STYLES, "bold"),
                  font_size=_int(req, "font_size", *FONT_SIZE),
                  align=_choice(req, "align", ALIGNS, "center"))
    elif kind == "qr":
        kw.update(data=_str(req, "data", MAX_DATA),
                  caption=_str(req, "caption", MAX_CAPTION) or None,
                  caption_side=_choice(req, "caption_side", CAPTION_SIDES, "right"))
    elif kind == "barcode":
        kw.update(data=_str(req, "data", MAX_DATA),
                  barcode_kind=_choice(req, "barcode_kind", _barcode_kinds(), "code128"),
                  show_text=_bool(req, "show_text", True))
    elif kind == "image":
        kw.update(image=decode_image(req.get("image")), dither=_bool(req, "dither", True))
    out.compose_kwargs = kw
    return out


def trim_request(req) -> tuple[float, float, float, float]:
    req = _require_dict(req)
    w = _number(req, "width_mm", *WIDTH_MM)
    h = _number(req, "height_mm", *HEIGHT_MM)
    if w is None or h is None:
        raise ValidationError("width_mm" if w is None else "height_mm", "required")
    return (w, h, _number(req, "top_mm", *TRIM_MM, default=0.0),
            _number(req, "bottom_mm", *TRIM_MM, default=0.0))


def scan_timeout(req) -> float:
    return _number(_require_dict(req), "timeout", *SCAN_TIMEOUT, default=5.0)


def connect_address(req) -> str | None:
    return _str(_require_dict(req), "address", MAX_ADDRESS) or None


def auto_reconnect(req) -> bool | None:
    req = _require_dict(req)
    return _bool(req, "auto_reconnect", False) if "auto_reconnect" in req else None
