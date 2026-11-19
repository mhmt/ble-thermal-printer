"""CLI messages. English by default; Turkish via `--lang tr` or `BLE_THERMAL_PRINTER_LANG=tr`.

Library code raises coded errors (PrinterError / RenderError) with English text;
the CLI translates them by code. The web UI has its own dictionary in web.html.
"""
from __future__ import annotations

import os

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # argparse
        "description": "Desktop client for Eleph-label compatible Bluetooth thermal label printers (e.g. MPIA P1)",
        "epilog": (
            "examples:\n"
            "  ble-thermal-printer scan\n"
            "  ble-thermal-printer info\n"
            "  ble-thermal-printer text \"Hello\\nWorld\" --border\n"
            "  ble-thermal-printer qr \"https://example.com\" --caption \"Example\"\n"
            "  ble-thermal-printer barcode 8690000000011 --kind ean13\n"
            "  ble-thermal-printer image logo.png --no-dither\n"
            "  ble-thermal-printer text \"Test\" --preview out.png   # save a PNG instead of printing\n"
            "  ble-thermal-printer serve                            # web interface"
        ),
        "h_verbose": "more logging (-v info, -vv debug)",
        "h_lang": "output language: en (default) or tr; also BLE_THERMAL_PRINTER_LANG",
        "h_scan": "list nearby compatible printers",
        "h_timeout": "scan duration in seconds",
        "h_info": "printer information and status",
        "h_raw": "also print the raw device info bytes",
        "h_serve": "start the web interface",
        "h_port": "HTTP port",
        "h_host": "HTTP host",
        "h_no_browser": "don't open a browser",
        "h_no_autoconnect": "don't connect to the printer on startup",
        "h_trusted_proxy": ("serve the web interface only to this address and skip the Host check "
                            "(Home Assistant Ingress: 172.30.32.2)"),
        "h_api_port": "extra port that serves only /api/v1 (token required)",
        "h_hass": ("connect to Home Assistant: its WebSocket URL with a token in HASS_TOKEN, or 'supervisor' "
                   "inside an add-on; prints on label_printer_print events"),
        "h_hass_entity": "state entity to publish (default sensor.label_printer; '' turns it off)",
        "h_token": "manage API tokens for /api/v1 (webhooks, scripts, Home Assistant)",
        "h_token_create": "create a token (shown once)",
        "h_token_name": "a name for the client, e.g. home-assistant",
        "h_scope": "print: print, preview and status; read: preview and status only",
        "h_daily_labels": "labels this token may print per day (0 = no limit)",
        "h_rate": "requests per minute",
        "h_token_list": "list tokens",
        "h_token_revoke": "revoke a token",
        "h_token_id": "token id or name",
        "h_public_host": ("host name under which the server is reachable from the internet "
                          "(e.g. a Tailscale Funnel name); only /api/v1 is served there (repeatable)"),
        "token_created": "Created token '{name}' (id {id}, scope {scope}). Copy it now; it is not stored and can't be shown again:",
        "token_none": "No API tokens. Create one with: ble-thermal-printer token create NAME",
        "token_header": "ID        NAME                 SCOPE   LABELS/DAY  REQ/MIN  CREATED",
        "token_revoked": "Revoked token '{name}' ({id}).",
        "h_allowed_host": ("extra host name accepted in the Host header (repeatable; e.g. printer.local); "
                           "'*' accepts any"),
        "h_calibrate": "print an alignment test label (mm rulers + edge lines)",
        "h_text": "text label",
        "h_text_arg": "text; use \\n for line breaks",
        "h_font_size": "fixed font size in px (default: fit to label)",
        "h_font_style": "font style",
        "h_font": "path to a TTF font file",
        "h_align": "text alignment",
        "h_image": "image label (PNG, JPG, …)",
        "h_path": "image file",
        "h_no_dither": "threshold instead of dithering (for logos/line art)",
        "h_qr": "QR code label",
        "h_qr_data": "QR content",
        "h_caption": "text next to / below the QR code",
        "h_caption_side": "caption position",
        "h_barcode": "barcode label (code128, ean13, …)",
        "h_bc_data": "barcode data",
        "h_kind": "barcode type",
        "h_no_text": "don't print the value below the bars",
        "h_size": "label size in mm (default: label_size from the config, else 40x30)",
        "h_margin": "margin in mm",
        "h_density": "print density 1-15 (default: device setting)",
        "h_copies": "number of copies",
        "h_rotate": "rotate the output (90 for text along a long label)",
        "h_preview": "don't print; save the preview as PNG",
        "h_printer": "BLE name or address (default: first compatible printer found)",
        "h_border": "draw a border around the label",
        "h_invert": "invert colors (black background)",
        "h_trim_top": "unprintable band at the top in mm (default: saved calibration for this size)",
        "h_trim_bottom": "unprintable band at the bottom in mm (default: saved calibration for this size)",
        "bad_size": "size must be WIDTHxHEIGHT, e.g. 40x30",
        # output
        "printer_line": "Printer: {name}  {width} dots, {dpi} DPI, mode={mode}",
        "sending": "Sending {pct:3d}% ({done}/{total} bytes)",
        "done": "Done.",
        "none_found": "No compatible printer found.",
        "preview_saved": "Preview saved: {path} ({w}x{h} px)",
        "error": "Error: {msg}",
        "i_name": "Name",
        "i_firmware": "Firmware",
        "i_width": "Width",
        "i_width_v": "{w} dots ({mm:g} mm)",
        "i_res": "Resolution",
        "i_mode": "Command mode",
        "i_speed_density": "Speed/Density",
        "i_paper": "Paper type",
        "i_compression": "Compression",
        "i_status": "Status",
        "i_raw": "Raw",
        "yes": "yes",
        "no": "no",
        "ready": "ready",
        "serve_url": "ble-thermal-printer web interface: {url}  (Ctrl+C to stop)",
        "serve_exposed": ("Warning: listening on a network address. The web interface has no login yet; "
                          "anyone who can reach this port can print and change settings."),
        # printer status codes
        "s_cover_open": "cover open",
        "s_paper_out": "out of paper",
        "s_overheated": "overheated",
        "s_cutter_error": "cutter error",
        # errors by code
        "e_not_found": "Printer not found. Is it on? If the phone app is connected, disconnect it first.",
        "e_timeout": "Timed out: the printer didn't respond",
        "e_not_ready": "Printer not ready: {list}",
        "e_esc_unsupported": "The device is in ESC mode; only TSPL is supported for now.",
        "e_too_wide": "Image is {width} dots wide, the printer prints at most {max} dots.",
        "e_barcode_too_wide": "Barcode doesn't fit the label ({modules} modules > {width} px)",
        "e_no_image": "No image selected",
        "e_unknown_kind": "Unknown label type: {kind}",
        "e_trim_too_large": "The unprintable area leaves no room for content",
        "e_token_bad_name": "Token name must be 1-{max} characters",
        "e_token_bad_scope": "Scope must be print or read",
        "e_token_bad_limit": "Limits must be positive",
        "e_token_exists": "A token named '{name}' already exists",
        "e_token_not_found": "No token with id or name '{token}'",
    },
    "tr": {
        "description": "Eleph-label uyumlu Bluetooth termal etiket yazıcıları için masaüstü istemci (ör. MPIA P1)",
        "epilog": (
            "örnekler:\n"
            "  ble-thermal-printer scan\n"
            "  ble-thermal-printer info\n"
            "  ble-thermal-printer text \"Merhaba\\nDünya\" --border\n"
            "  ble-thermal-printer qr \"https://example.com\" --caption \"Örnek\"\n"
            "  ble-thermal-printer barcode 8690000000011 --kind ean13\n"
            "  ble-thermal-printer image logo.png --no-dither\n"
            "  ble-thermal-printer text \"Deneme\" --preview out.png  # yazdırmadan PNG kaydet\n"
            "  ble-thermal-printer serve                            # web arayüzü"
        ),
        "h_verbose": "daha fazla log (-v bilgi, -vv hata ayıklama)",
        "h_lang": "çıktı dili: en (varsayılan) ya da tr; BLE_THERMAL_PRINTER_LANG ile de ayarlanır",
        "h_scan": "yakındaki uyumlu yazıcıları listele",
        "h_timeout": "tarama süresi (sn)",
        "h_info": "yazıcı bilgisi ve durumu",
        "h_raw": "ham cihaz bilgisi baytlarını da yazdır",
        "h_serve": "web arayüzünü başlat",
        "h_port": "HTTP portu",
        "h_host": "HTTP adresi",
        "h_no_browser": "tarayıcı açma",
        "h_no_autoconnect": "açılışta yazıcıya bağlanma",
        "h_trusted_proxy": ("web arayüzünü yalnızca bu adrese sun ve Host kontrolünü atla "
                            "(Home Assistant Ingress: 172.30.32.2)"),
        "h_api_port": "yalnızca /api/v1 sunan ek port (token gerekir)",
        "h_hass": ("Home Assistant'a bağlan: WebSocket URL'si (token HASS_TOKEN'da) ya da add-on içinde "
                   "'supervisor'; label_printer_print olaylarında baskı alır"),
        "h_hass_entity": "yayınlanacak durum entity'si (varsayılan sensor.label_printer; '' kapatır)",
        "h_token": "/api/v1 için API token'larını yönet (webhook, script, Home Assistant)",
        "h_token_create": "token oluştur (yalnızca bir kez gösterilir)",
        "h_token_name": "istemci için bir ad, örn. home-assistant",
        "h_scope": "print: baskı, önizleme ve durum; read: yalnızca önizleme ve durum",
        "h_daily_labels": "bu token'ın günde basabileceği etiket sayısı (0 = sınırsız)",
        "h_rate": "dakikadaki istek sayısı",
        "h_token_list": "token'ları listele",
        "h_token_revoke": "token'ı iptal et",
        "h_token_id": "token id'si ya da adı",
        "h_public_host": ("sunucunun internetten erişildiği ad (örn. Tailscale Funnel adı); "
                          "bu adla yalnızca /api/v1 sunulur (tekrarlanabilir)"),
        "token_created": "'{name}' token'ı oluşturuldu (id {id}, kapsam {scope}). Şimdi kopyalayın; saklanmıyor ve tekrar gösterilemez:",
        "token_none": "API token'ı yok. Oluşturmak için: ble-thermal-printer token create AD",
        "token_header": "ID        AD                   KAPSAM  ETİKET/GÜN  İST/DK   OLUŞTURMA",
        "token_revoked": "'{name}' token'ı iptal edildi ({id}).",
        "h_allowed_host": ("Host başlığında kabul edilecek ek ad (tekrarlanabilir; örn. printer.local); "
                           "'*' hepsini kabul eder"),
        "h_calibrate": "hizalama test etiketi bas (mm cetveli + kenar çizgileri)",
        "h_text": "metin etiketi",
        "h_text_arg": "metin; satır sonu için \\n",
        "h_font_size": "sabit font boyutu (px). Vars: etikete sığdır",
        "h_font_style": "yazı stili",
        "h_font": "TTF font dosyası yolu",
        "h_align": "metin hizalaması",
        "h_image": "resim etiketi (PNG, JPG, …)",
        "h_path": "resim dosyası",
        "h_no_dither": "tonlama yerine eşikleme (logo/çizim için)",
        "h_qr": "QR kod etiketi",
        "h_qr_data": "QR içeriği",
        "h_caption": "QR yanına/altına metin",
        "h_caption_side": "açıklama yeri",
        "h_barcode": "barkod etiketi (code128, ean13, …)",
        "h_bc_data": "barkod verisi",
        "h_kind": "barkod türü",
        "h_no_text": "çubukların altına değeri yazma",
        "h_size": "etiket boyutu mm (vars. config'teki label_size, yoksa 40x30)",
        "h_margin": "kenar boşluğu mm",
        "h_density": "baskı koyuluğu 1-15 (vars. cihaz ayarı)",
        "h_copies": "kopya sayısı",
        "h_rotate": "çıktıyı döndür (uzun etiketlerde yatay yazı için 90)",
        "h_preview": "yazdırma, önizlemeyi PNG olarak kaydet",
        "h_printer": "BLE adı veya adresi (vars. ilk bulunan uyumlu yazıcı)",
        "h_border": "etiket çevresine çerçeve",
        "h_invert": "renkleri ters çevir (siyah zemin)",
        "h_trim_top": "üstte basılamayan alan, mm (vars. bu boyut için kayıtlı kalibrasyon)",
        "h_trim_bottom": "altta basılamayan alan, mm (vars. bu boyut için kayıtlı kalibrasyon)",
        "bad_size": "boyut GENİŞLİKxYÜKSEKLİK biçiminde olmalı, ör. 40x30",
        "printer_line": "Yazıcı: {name}  {width} nokta, {dpi} DPI, mod={mode}",
        "sending": "Gönderiliyor {pct:3d}% ({done}/{total} bayt)",
        "done": "Tamam.",
        "none_found": "Uyumlu yazıcı bulunamadı.",
        "preview_saved": "Önizleme kaydedildi: {path} ({w}x{h} px)",
        "error": "Hata: {msg}",
        "i_name": "Ad",
        "i_firmware": "Firmware",
        "i_width": "Genişlik",
        "i_width_v": "{w} nokta ({mm:g} mm)",
        "i_res": "Çözünürlük",
        "i_mode": "Komut modu",
        "i_speed_density": "Hız/Koyuluk",
        "i_paper": "Kağıt tipi",
        "i_compression": "Sıkıştırma",
        "i_status": "Durum",
        "i_raw": "Ham",
        "yes": "evet",
        "no": "hayır",
        "ready": "hazır",
        "serve_url": "ble-thermal-printer web arayüzü: {url}  (durdurmak için Ctrl+C)",
        "serve_exposed": ("Uyarı: ağ adresinde dinleniyor. Web arayüzünde henüz giriş yok; "
                          "bu porta erişebilen herkes baskı alabilir ve ayarları değiştirebilir."),
        "s_cover_open": "kapak açık",
        "s_paper_out": "kağıt yok",
        "s_overheated": "aşırı ısınma",
        "s_cutter_error": "kesici hatası",
        "e_not_found": "Yazıcı bulunamadı. Açık mı? Telefon uygulaması bağlıysa önce bağlantıyı kesin.",
        "e_timeout": "Zaman aşımı: yazıcı cevap vermedi",
        "e_not_ready": "Yazıcı hazır değil: {list}",
        "e_esc_unsupported": "Cihaz ESC modunda; şimdilik yalnızca TSPL destekleniyor.",
        "e_too_wide": "Görüntü {width} nokta, yazıcı en fazla {max} nokta basabilir.",
        "e_barcode_too_wide": "Barkod etikete sığmıyor ({modules} modül > {width} px)",
        "e_no_image": "Resim seçilmedi",
        "e_unknown_kind": "Bilinmeyen etiket türü: {kind}",
        "e_trim_too_large": "Basılamayan alan içeriğe yer bırakmıyor",
        "e_token_bad_name": "Token adı 1-{max} karakter olmalı",
        "e_token_bad_scope": "Kapsam print ya da read olmalı",
        "e_token_bad_limit": "Limitler pozitif olmalı",
        "e_token_exists": "'{name}' adında bir token zaten var",
        "e_token_not_found": "'{token}' id'li ya da adlı token yok",
    },
}

LANGS = tuple(MESSAGES)
_lang = "en"


def detect_lang(argv: list[str]) -> str:
    """--lang X / --lang=X in argv, then BLE_THERMAL_PRINTER_LANG, else English."""
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            cand = argv[i + 1]
            break
        if a.startswith("--lang="):
            cand = a.split("=", 1)[1]
            break
    else:
        cand = os.environ.get("BLE_THERMAL_PRINTER_LANG", "en")
    cand = cand.lower()[:2]
    return cand if cand in MESSAGES else "en"


def set_lang(lang: str) -> None:
    global _lang
    _lang = lang if lang in MESSAGES else "en"


def t(key: str, **params) -> str:
    s = MESSAGES[_lang].get(key) or MESSAGES["en"].get(key) or key
    return s.format(**params) if params else s


def status_list(codes) -> str:
    return ", ".join(t(f"s_{c}") for c in codes)


def error_text(e: BaseException) -> str:
    """Translate a coded error; fall back to its own message."""
    code = getattr(e, "code", None)
    if code is None and isinstance(e, TimeoutError):
        code = "timeout"
    if code and f"e_{code}" in MESSAGES["en"]:
        params = dict(getattr(e, "params", {}) or {})
        params.setdefault("list", status_list(params.get("errors", [])))
        try:
            return t(f"e_{code}", **params)
        except (KeyError, IndexError):
            pass
    return str(e) or e.__class__.__name__
