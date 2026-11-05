"""ble-thermal-printer command line. English by default; `--lang tr` or `BLE_THERMAL_PRINTER_LANG=tr` for Turkish."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from PIL import Image

from . import config, render
from .i18n import LANGS, detect_lang, error_text, set_lang, status_list, t


def parse_size(s: str) -> tuple[float, float]:
    try:
        w, h = s.lower().replace("mm", "").split("x")
        return float(w), float(h)
    except ValueError:
        raise argparse.ArgumentTypeError(t("bad_size"))


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--size", type=parse_size, default=None, help=t("h_size"))
    p.add_argument("--margin", type=float, default=1.5, help=t("h_margin"))
    p.add_argument("--density", type=int, help=t("h_density"))
    p.add_argument("--copies", type=int, default=1, help=t("h_copies"))
    p.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0, help=t("h_rotate"))
    p.add_argument("--preview", metavar="PNG", help=t("h_preview"))
    p.add_argument("--printer", help=t("h_printer"))
    p.add_argument("--border", action="store_true", help=t("h_border"))
    p.add_argument("--invert", action="store_true", help=t("h_invert"))
    p.add_argument("--trim-top", type=float, metavar="MM", help=t("h_trim_top"))
    p.add_argument("--trim-bottom", type=float, metavar="MM", help=t("h_trim_bottom"))


def build_image(args) -> tuple[Image.Image, render.Label]:
    w, h = args.size or config.get_label_size()
    top, bottom = config.get_trim(w, h)
    common = dict(width_mm=w, height_mm=h, margin_mm=args.margin, rotate=args.rotate,
                  border=args.border, invert=args.invert,
                  trim_top_mm=top if args.trim_top is None else args.trim_top,
                  trim_bottom_mm=bottom if args.trim_bottom is None else args.trim_bottom)
    if args.cmd == "calibrate":
        return render.render_calibration(w, h)
    if args.cmd == "text":
        return render.compose("text", text=args.text.replace("\\n", "\n"), font_style=args.font_style,
                              font_size=args.font_size, font_path=args.font, align=args.align, **common)
    if args.cmd == "image":
        return render.compose("image", image=args.path, dither=not args.no_dither, **common)
    if args.cmd == "qr":
        return render.compose("qr", data=args.data, caption=args.caption,
                              caption_side=args.caption_side, **common)
    if args.cmd == "barcode":
        return render.compose("barcode", data=args.data, barcode_kind=args.kind,
                              show_text=not args.no_text, **common)
    raise render.RenderError("unknown_kind", f"Unknown label type: {args.cmd}", kind=args.cmd)


async def do_print(img: Image.Image, label: render.Label, args) -> None:
    from .ble import Printer

    async with Printer(args.printer) as p:
        info = await p.get_info()
        print(t("printer_line", name=p.name, width=info.width, dpi=info.dpi, mode=info.mode_name))

        def prog(done, total):
            print("\r" + t("sending", pct=done * 100 // total, done=done, total=total), end="", flush=True)

        await p.print_image(img, label.width_mm, label.height_mm, density=args.density,
                            copies=args.copies, progress=prog)
        print("\n" + t("done"))


async def cmd_scan(args) -> None:
    from .ble import scan

    devs = await scan(args.timeout)
    if not devs:
        print(t("none_found"))
    for d, rssi in devs:
        print(f"{d.name}\t{d.address}\trssi={rssi}")


async def cmd_info(args) -> None:
    from .ble import Printer

    async with Printer(args.printer) as p:
        info = await p.get_info()
        st = await p.get_status()
        rows = [
            (t("i_name"), p.name),
            (t("i_firmware"), info.version),
            (t("i_width"), t("i_width_v", w=info.width, mm=info.width / info.dots_per_mm)),
            (t("i_res"), f"{info.dpi} DPI"),
            (t("i_mode"), info.mode_name),
            (t("i_speed_density"), f"{info.speed} / {info.density}"),
            (t("i_paper"), info.paper_type),
            (t("i_compression"), t("yes") if info.compression else t("no")),
            (t("i_status"), f"{st.raw.hex(' ')} ({status_list(st.codes) or t('ready')})"),
        ]
        if args.raw:
            rows.append((t("i_raw"), info.raw.hex(" ")))
        width = max(len(k) for k, _ in rows)
        for k, v in rows:
            print(f"{k.ljust(width)} : {v}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ble-thermal-printer", description=t("description"), epilog=t("epilog"),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="count", default=0, help=t("h_verbose"))
    ap.add_argument("--lang", choices=LANGS, help=t("h_lang"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help=t("h_scan"))
    s.add_argument("--timeout", type=float, default=6, help=t("h_timeout"))

    s = sub.add_parser("info", help=t("h_info"))
    s.add_argument("--printer", help=t("h_printer"))
    s.add_argument("--raw", action="store_true", help=t("h_raw"))

    s = sub.add_parser("serve", help=t("h_serve"))
    s.add_argument("--port", type=int, default=8765, help=t("h_port"))
    s.add_argument("--host", default="127.0.0.1", help=t("h_host"))
    s.add_argument("--printer", help=t("h_printer"))
    s.add_argument("--no-browser", action="store_true", help=t("h_no_browser"))
    s.add_argument("--no-autoconnect", action="store_true", help=t("h_no_autoconnect"))

    s = sub.add_parser("calibrate", help=t("h_calibrate"))
    add_common(s)

    s = sub.add_parser("text", help=t("h_text"))
    s.add_argument("text", help=t("h_text_arg"))
    s.add_argument("--font-size", type=int, help=t("h_font_size"))
    s.add_argument("--font-style", choices=["regular", "bold", "mono"], default="bold", help=t("h_font_style"))
    s.add_argument("--font", help=t("h_font"))
    s.add_argument("--align", choices=["left", "center", "right"], default="center", help=t("h_align"))
    add_common(s)

    s = sub.add_parser("image", help=t("h_image"))
    s.add_argument("path", help=t("h_path"))
    s.add_argument("--no-dither", action="store_true", help=t("h_no_dither"))
    add_common(s)

    s = sub.add_parser("qr", help=t("h_qr"))
    s.add_argument("data", help=t("h_qr_data"))
    s.add_argument("--caption", help=t("h_caption"))
    s.add_argument("--caption-side", choices=["right", "bottom"], default="right", help=t("h_caption_side"))
    add_common(s)

    s = sub.add_parser("barcode", help=t("h_barcode"))
    s.add_argument("data", help=t("h_bc_data"))
    s.add_argument("--kind", default="code128", help=t("h_kind"))
    s.add_argument("--no-text", action="store_true", help=t("h_no_text"))
    add_common(s)
    return ap


def _strip_lang(argv: list[str]) -> list[str]:
    """--lang may appear anywhere (also after the subcommand); the top-level parser only sees it up front."""
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a == "--lang":
            skip = True
            continue
        if a.startswith("--lang="):
            continue
        out.append(a)
    return out


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    set_lang(detect_lang(argv))
    args = build_parser().parse_args(_strip_lang(argv))
    logging.basicConfig(level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
                        format="%(levelname)s %(message)s")

    try:
        if args.cmd == "scan":
            asyncio.run(cmd_scan(args))
        elif args.cmd == "info":
            asyncio.run(cmd_info(args))
        elif args.cmd == "serve":
            from .server import serve
            serve(args.host, args.port, args.printer, open_browser=not args.no_browser,
                  autoconnect=not args.no_autoconnect)
        else:
            img, label = build_image(args)
            if args.preview:
                img.save(args.preview)
                print(t("preview_saved", path=args.preview, w=img.width, h=img.height))
                return
            asyncio.run(do_print(img, label, args))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        if args.verbose >= 2:
            raise
        sys.exit(t("error", msg=error_text(e)))


if __name__ == "__main__":
    main()
