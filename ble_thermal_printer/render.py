"""Render label content (text, image, QR, barcode) to a 1-bit bitmap."""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont, ImageOps

class RenderError(ValueError):
    def __init__(self, code: str, message: str, **params):
        super().__init__(message)
        self.code = code
        self.params = params


FONT_CANDIDATES = {
    "regular": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",  # Alpine (font-dejavu)
        "C:/Windows/Fonts/arial.ttf",
    ],
    "bold": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
    "mono": [
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        "C:/Windows/Fonts/consola.ttf",
    ],
}


def load_font(style: str = "bold", size: int = 32, path: str | None = None) -> ImageFont.FreeTypeFont:
    if path:
        return ImageFont.truetype(path, size)
    for p in FONT_CANDIDATES.get(style, FONT_CANDIDATES["bold"]):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


@dataclass
class Label:
    width_mm: float = 40
    height_mm: float = 30
    dots_per_mm: int = 8
    margin_mm: float = 1.5
    max_width_px: int = 384  # print head: 384 dots = 48 mm
    max_height_px: int | None = None

    @property
    def size_px(self) -> tuple[int, int]:
        w = round(self.width_mm * self.dots_per_mm)
        w = min((w + 7) // 8 * 8, self.max_width_px)
        h = round(self.height_mm * self.dots_per_mm)
        if self.max_height_px:
            h = min(h, self.max_height_px)
        return w, h

    @property
    def margin_px(self) -> int:
        return round(self.margin_mm * self.dots_per_mm)

    def canvas(self) -> Image.Image:
        return Image.new("L", self.size_px, 255)

    def inner_box(self) -> tuple[int, int, int, int]:
        w, h = self.size_px
        m = self.margin_px
        return m, m, w - m, h - m


# --- Helpers ---------------------------------------------------------------
def to_1bit(img: Image.Image, dither: bool = True, threshold: int = 128) -> Image.Image:
    g = img.convert("L")
    if dither:
        return g.convert("1")  # Floyd–Steinberg
    return g.point(lambda v: 255 if v >= threshold else 0, mode="1")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        cur = ""
        for word in words:
            trial = word if not cur else f"{cur} {word}"
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _text_block(text: str, box_w: int, box_h: int, *, style: str, font_size: int | None,
                font_path: str | None, wrap: bool, line_spacing: float):
    """Find the largest font size that fits the box. Returns (font, lines, line height)."""
    scratch = ImageDraw.Draw(Image.new("L", (1, 1)))

    def layout(size: int):
        font = load_font(style, size, font_path)
        lines = _wrap(scratch, text, font, box_w) if wrap else text.split("\n")
        asc, desc = font.getmetrics()
        lh = int((asc + desc) * line_spacing)
        width = max(scratch.textlength(l, font=font) for l in lines) if lines else 0
        height = lh * len(lines) - int((asc + desc) * (line_spacing - 1))
        return font, lines, lh, width, height

    if font_size:
        return layout(font_size)[:3]
    lo, hi, best = 6, max(box_h, 8), None
    while lo <= hi:
        mid = (lo + hi) // 2
        font, lines, lh, w, h = layout(mid)
        if w <= box_w and h <= box_h:
            best = (font, lines, lh)
            lo = mid + 1
        else:
            hi = mid - 1
    return best or layout(6)[:3]


def draw_text(img: Image.Image, box: tuple[int, int, int, int], text: str, *, style="bold",
              font_size: int | None = None, font_path: str | None = None, align="center",
              valign="middle", wrap=True, line_spacing=1.05) -> None:
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    font, lines, lh = _text_block(text, bw, bh, style=style, font_size=font_size,
                                  font_path=font_path, wrap=wrap, line_spacing=line_spacing)
    asc, desc = font.getmetrics()
    total_h = lh * (len(lines) - 1) + asc + desc
    y = {"top": y0, "bottom": y1 - total_h}.get(valign, y0 + (bh - total_h) // 2)
    d = ImageDraw.Draw(img)
    for line in lines:
        lw = d.textlength(line, font=font)
        x = {"left": x0, "right": x1 - lw}.get(align, x0 + (bw - lw) / 2)
        d.text((x, y), line, font=font, fill=0)
        y += lh


def paste_fit(img: Image.Image, box: tuple[int, int, int, int], src: Image.Image,
              *, dither=True, sharp=False) -> None:
    """Fit src into the box, keeping its aspect ratio, and center it."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    src = ImageOps.exif_transpose(src)
    if src.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGBA", src.size, "white")
        src = Image.alpha_composite(bg, src.convert("RGBA"))
    src = src.convert("L")
    scale = min(bw / src.width, bh / src.height)
    new = (max(1, int(src.width * scale)), max(1, int(src.height * scale)))
    src = src.resize(new, Image.NEAREST if sharp else Image.LANCZOS)
    if not sharp:
        src = to_1bit(src, dither=dither).convert("L")
    img.paste(src, (x0 + (bw - new[0]) // 2, y0 + (bh - new[1]) // 2))


# --- Label templates -------------------------------------------------------
def render_text(label: Label, text: str, **kw) -> Image.Image:
    img = label.canvas()
    draw_text(img, label.inner_box(), text, **kw)
    return img


def render_image(label: Label, path_or_img, *, dither=True, rotate: int = 0) -> Image.Image:
    src = Image.open(path_or_img) if not isinstance(path_or_img, Image.Image) else path_or_img
    if rotate:
        src = src.rotate(rotate, expand=True)
    img = label.canvas()
    paste_fit(img, label.inner_box(), src, dither=dither)
    return img


def make_qr(data: str, border: int = 0) -> Image.Image:
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=border, box_size=1)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").get_image().convert("L")


def _paste_integer_scaled(img: Image.Image, box, src: Image.Image) -> None:
    """Place module-based graphics (QR, barcodes) at an integer scale so they stay sharp."""
    x0, y0, x1, y1 = box
    k = max(1, min((x1 - x0) // src.width, (y1 - y0) // src.height))
    src = src.resize((src.width * k, src.height * k), Image.NEAREST)
    img.paste(src, (x0 + (x1 - x0 - src.width) // 2, y0 + (y1 - y0 - src.height) // 2))


def render_qr(label: Label, data: str, caption: str | None = None, *, caption_side="right") -> Image.Image:
    img = label.canvas()
    x0, y0, x1, y1 = label.inner_box()
    qr = make_qr(data)
    if not caption:
        _paste_integer_scaled(img, (x0, y0, x1, y1), qr)
        return img
    gap = label.dots_per_mm
    if caption_side == "bottom":
        qh = int((y1 - y0) * 0.72)
        _paste_integer_scaled(img, (x0, y0, x1, y0 + qh), qr)
        draw_text(img, (x0, y0 + qh + gap // 2, x1, y1), caption)
    else:
        side = min(y1 - y0, int((x1 - x0) * 0.55))
        _paste_integer_scaled(img, (x0, y0, x0 + side, y1), qr)
        draw_text(img, (x0 + side + gap, y0, x1, y1), caption, align="left")
    return img


def render_barcode(label: Label, data: str, *, kind="code128", show_text=True) -> Image.Image:
    import barcode

    img = label.canvas()
    x0, y0, x1, y1 = label.inner_box()
    # build() returns the module string ("1" = bar); draw each module at an integer pixel width
    modules = "".join(barcode.get(kind, data).build()).strip("0")
    k = (x1 - x0) // len(modules)
    if k < 1:
        raise RenderError("barcode_too_wide", f"Barcode doesn't fit the label ({len(modules)} modules > {x1 - x0} px)",
                          modules=len(modules), width=x1 - x0)
    text_h = int((y1 - y0) * 0.25) if show_text else 0
    bar_w = len(modules) * k
    left = x0 + (x1 - x0 - bar_w) // 2
    d = ImageDraw.Draw(img)
    for i, m in enumerate(modules):
        if m == "1":
            d.rectangle((left + i * k, y0, left + (i + 1) * k - 1, y1 - text_h - 1), fill=0)
    if show_text:
        draw_text(img, (x0, y1 - text_h + 2, x1, y1), data, style="regular")
    return img


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("1").save(buf, "PNG")
    return buf.getvalue()


# --- Shared label composer (CLI + web) -------------------------------------
KINDS = ("text", "qr", "barcode", "image")
BORDER_INSET_MM = 1.0  # the very first/last dot rows may not land on the label


def effective_margin_mm(margin_mm: float, border: bool) -> float:
    """Content margin actually used: with a border, content must clear the inset border line."""
    return max(margin_mm, BORDER_INSET_MM + 1.5) if border else margin_mm


def compose(kind: str, *, width_mm: float, height_mm: float, margin_mm: float = 1.5,
            rotate: int = 0, border: bool = False, invert: bool = False,
            text: str = "", font_style: str = "bold", font_size: int | None = None,
            font_path: str | None = None, align: str = "center",
            data: str = "", caption: str | None = None, caption_side: str = "right",
            barcode_kind: str = "code128", show_text: bool = True,
            image: "Image.Image | str | None" = None, dither: bool = True,
            trim_top_mm: float = 0.0, trim_bottom_mm: float = 0.0) -> tuple[Image.Image, Label]:
    """Lay out the label and return the 1-bit image to send to the printer.

    trim_top_mm / trim_bottom_mm: bands the printer can't put on the label (measured with
    `ble-thermal-printer calibrate`). Content, margins and border are laid out in the remaining area.
    """
    if trim_top_mm > 0 or trim_bottom_mm > 0:
        inner_h = height_mm - trim_top_mm - trim_bottom_mm
        if inner_h < 3:
            raise RenderError("trim_too_large", "Unprintable area leaves no room for content")
        inner, _ = compose(kind, width_mm=width_mm, height_mm=inner_h, margin_mm=margin_mm, rotate=rotate,
                           border=border, invert=invert, text=text, font_style=font_style,
                           font_size=font_size, font_path=font_path, align=align, data=data,
                           caption=caption, caption_side=caption_side, barcode_kind=barcode_kind,
                           show_text=show_text, image=image, dither=dither)
        target = Label(width_mm, height_mm, margin_mm=margin_mm)
        canvas = target.canvas()
        canvas.paste(inner.convert("L"), (0, round(trim_top_mm * target.dots_per_mm)))
        return to_1bit(canvas, dither=False), target
    target = Label(width_mm, height_mm, margin_mm=margin_mm)
    tw, th = target.size_px
    margin_mm = effective_margin_mm(margin_mm, border)
    if rotate in (90, 270):
        design = Label(height_mm, width_mm, margin_mm=margin_mm, max_width_px=th, max_height_px=tw)
    else:
        design = Label(width_mm, height_mm, margin_mm=margin_mm)

    if kind == "text":
        img = render_text(design, text, style=font_style, font_size=font_size,
                          font_path=font_path, align=align)
    elif kind == "qr":
        img = render_qr(design, data, caption or None, caption_side=caption_side)
    elif kind == "barcode":
        img = render_barcode(design, data, kind=barcode_kind, show_text=show_text)
    elif kind == "image":
        if image is None:
            raise RenderError("no_image", "No image selected")
        img = render_image(design, image, dither=dither)
    else:
        raise RenderError("unknown_kind", f"Unknown label type: {kind}", kind=kind)

    if rotate:
        img = img.rotate(-rotate, expand=True)
    canvas = target.canvas()
    canvas.paste(img, ((tw - img.width) // 2, (th - img.height) // 2))
    img = canvas
    if border:
        k = round(BORDER_INSET_MM * target.dots_per_mm)
        ImageDraw.Draw(img).rectangle((k, k, tw - 1 - k, th - 1 - k), outline=0, width=2)
    img = to_1bit(img, dither=False)
    if invert:
        img = ImageOps.invert(img.convert("L")).convert("1")
    return img, target


def render_calibration(width_mm: float, height_mm: float) -> tuple[Image.Image, Label]:
    """Alignment test: mm rulers on both sides (0 = top of the image) plus edge lines.

    Read off the top-most and bottom-most fully visible tick on the printed label to
    find how far the printer shifts the image vertically.
    """
    label = Label(width_mm, height_mm, margin_mm=0)
    img = label.canvas()
    w, h = img.size
    dpm = label.dots_per_mm
    d = ImageDraw.Draw(img)
    font = load_font("bold", 13)
    for mm in range(0, int(h / dpm) + 1):
        y = min(mm * dpm, h - 1)
        long = mm % 2 == 0
        tick = 28 if long else 14
        d.line((0, y, tick, y), fill=0, width=1)
        d.line((w - 1 - tick, y, w - 1, y), fill=0, width=1)
        if long and 0 < mm < h / dpm:
            txt = str(mm)
            tw = d.textlength(txt, font=font)
            d.text((32, y - 7), txt, font=font, fill=0)
            d.text((w - 33 - tw, y - 7), txt, font=font, fill=0)
    # edge lines: row 0 and last row, full width minus rulers
    d.line((60, 0, w - 61, 0), fill=0, width=1)
    d.line((60, h - 1, w - 61, h - 1), fill=0, width=1)
    # centre cross for horizontal alignment
    d.line((w // 2, h // 2 - 12, w // 2, h // 2 + 12), fill=0, width=1)
    d.line((w // 2 - 12, h // 2, w // 2 + 12, h // 2), fill=0, width=1)
    small = load_font("bold", 14)
    d.text((w // 2, 4), "TOP", font=small, fill=0, anchor="mt")
    d.text((w // 2, h - 4), "BOTTOM", font=small, fill=0, anchor="mb")
    return to_1bit(img, dither=False), label
