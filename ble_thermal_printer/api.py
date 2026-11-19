"""Token-authenticated HTTP API for scripts, Home Assistant and webhooks: /api/v1/...

  GET  /api/v1/status    scope read   printer state
  POST /api/v1/preview   scope read   label JSON -> PNG
  POST /api/v1/print     scope print  label JSON -> printed now, or an error

Requests carry `Authorization: Bearer <token>` (see tokens.py). The label JSON is the same as
the web UI's (schema.label_request). Printing is synchronous: if the printer isn't connected or
ready, the call fails with 503 right away instead of being queued. An `Idempotency-Key` header
makes retries safe: a repeated key returns the first successful result without printing again.
Rate limits, daily label quotas and idempotency keys are kept in memory and reset on restart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from . import config, httpguard, render, schema, tokens
from .ble import PrinterError
from .manager import err_info
from .render import RenderError

log = logging.getLogger("ble_thermal_printer.api")

PREFIX = "/api/v1/"
MAX_COPIES = 10                 # per API request (the web UI allows 99)
PRINT_TIMEOUT = 90              # seconds to wait for the printer lock and the job
IDEMPOTENCY_TTL = 24 * 3600
IDEMPOTENCY_MAX = 1000
MAX_IDEMPOTENCY_KEY = 200

ROUTES = {
    ("GET", "status"): "read",
    ("POST", "preview"): "read",
    ("POST", "print"): "print",
}
UNAVAILABLE = {"not_connected", "not_ready", "lost", "timeout", "not_found", "esc_unsupported"}


def compose_label(req, *, max_copies: int):
    """Validated label JSON -> (1-bit image, Label, LabelRequest), with the calibrated trim applied."""
    lr = schema.label_request(req, max_copies=max_copies)
    if lr.kind == "calibrate":
        img, label = render.render_calibration(lr.width_mm, lr.height_mm)
    else:
        top, bottom = config.get_trim(lr.width_mm, lr.height_mm)
        img, label = render.compose(lr.kind, width_mm=lr.width_mm, height_mm=lr.height_mm,
                                    trim_top_mm=top, trim_bottom_mm=bottom, **lr.compose_kwargs)
    return img, label, lr


@dataclass
class Reply:
    status: int
    body: bytes
    ctype: str = "application/json; charset=utf-8"
    headers: dict = field(default_factory=dict)


def _json(status: int, obj: dict, headers: dict | None = None) -> Reply:
    return Reply(status, json.dumps(obj, ensure_ascii=False).encode(), headers=headers or {})


def _error(status: int, code: str, text: str, headers: dict | None = None, **params) -> Reply:
    return _json(status, {"ok": False, "error": {"code": code, "params": params, "text": text}}, headers)


@dataclass
class _Idem:
    fingerprint: str
    created: float
    reply: Reply | None = None  # None while the first request is still running


class ApiV1:
    def __init__(self, mgr, clock: Callable[[], float] = time.time):
        self.mgr = mgr
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}
        self._labels: dict[str, tuple[date, int]] = {}
        self._idem: OrderedDict[tuple[str, str], _Idem] = OrderedDict()

    # --- entry point ---------------------------------------------------------
    def handle(self, method: str, path: str, headers, read_body: Callable[[], bytes]) -> Reply:
        name = path[len(PREFIX):]
        scope = ROUTES.get((method, name))
        if scope is None:
            if any(n == name for _, n in ROUTES):
                return _error(405, "method_not_allowed", "Method not allowed")
            return _error(404, "not_found", "Unknown API endpoint")

        token = self._authenticate(headers)
        if token is None:  # checked before reading the body
            return _error(401, "unauthorized", "Missing or invalid API token",
                          {"WWW-Authenticate": 'Bearer realm="ble-thermal-printer"'})
        if not token.allows(scope):
            return _error(403, "insufficient_scope", f"This token can't use '{name}'", scope=token.scope)
        retry = self._rate_limit(token)
        if retry:
            return _error(429, "rate_limited", "Too many requests", {"Retry-After": str(retry)},
                          limit=token.rate_per_min)
        try:
            if name == "status":
                return _json(200, {"ok": True, "printer": self._printer_state()})
            raw = read_body()
            req = httpguard.parse_json(raw)
            if name == "preview":
                img, label, _ = self._compose(req)
                return Reply(200, render.to_png_bytes(img), "image/png")
            return self._print(token, headers, raw, req)
        except httpguard.Rejected as r:
            return _error(r.status, r.code, str(r))
        except Exception as e:
            return self._failure(e, name)

    # --- auth and limits ----------------------------------------------------
    @staticmethod
    def _authenticate(headers) -> tokens.Token | None:
        auth = headers.get("Authorization") or ""
        kind, _, secret = auth.partition(" ")
        if kind.lower() != "bearer" or not secret.strip():
            return None
        return tokens.verify(secret.strip())

    def _rate_limit(self, token: tokens.Token) -> int:
        """0 if allowed, else seconds until the next request is allowed."""
        now = self.clock()
        with self._lock:
            hits = self._hits.setdefault(token.id, deque())
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= token.rate_per_min:
                return max(1, int(60 - (now - hits[0])) + 1)
            hits.append(now)
            return 0

    def _labels_used(self, token: tokens.Token) -> int:
        day, used = self._labels.get(token.id, (None, 0))
        return used if day == date.fromtimestamp(self.clock()) else 0

    def _reserve_labels(self, token: tokens.Token, n: int) -> int | None:
        """Count n labels against today's quota; returns the previous count if over quota."""
        with self._lock:
            used = self._labels_used(token)
            if token.daily_labels and used + n > token.daily_labels:
                return used
            self._labels[token.id] = (date.fromtimestamp(self.clock()), used + n)
            return None

    def _refund_labels(self, token: tokens.Token, n: int) -> None:
        with self._lock:
            used = self._labels_used(token)
            self._labels[token.id] = (date.fromtimestamp(self.clock()), max(0, used - n))

    # --- endpoints -----------------------------------------------------------
    def _printer_state(self) -> dict:
        s = self.mgr.snapshot()
        status = s.get("status") or {}
        connected = s.get("phase") in ("connected", "printing")
        return {
            "phase": s.get("phase"), "connected": connected,
            "ready": bool(connected and status.get("ready")),
            "errors": status.get("errors") or [],
            "name": (s.get("device") or {}).get("name"),
            "label_size_mm": list(config.get_label_size()),
        }

    @staticmethod
    def _compose(req):
        return compose_label(req, max_copies=MAX_COPIES)

    def _print(self, token: tokens.Token, headers, raw: bytes, req) -> Reply:
        key = (headers.get("Idempotency-Key") or "").strip()
        if key:
            if len(key) > MAX_IDEMPOTENCY_KEY or not key.isascii() or not key.isprintable():
                raise schema.ValidationError("Idempotency-Key", f"at most {MAX_IDEMPOTENCY_KEY} printable ASCII characters")
            replay = self._idem_begin(token, key, hashlib.sha256(raw).hexdigest())
            if replay is not None:
                return replay
        try:
            reply = self._do_print(token, req)
        except BaseException:
            if key:
                self._idem_abort(token, key)
            raise
        if key:
            self._idem_finish(token, key, reply)
        return reply

    def _do_print(self, token: tokens.Token, req) -> Reply:
        img, label, lr = self._compose(req)
        if not self._printer_state()["connected"]:  # fail fast; manager.print re-checks under its lock
            raise PrinterError("not_connected", "Printer is not connected")
        used = self._reserve_labels(token, lr.copies)
        if used is not None:
            return _error(429, "quota_exceeded", "Daily label quota exceeded",
                          used=used, limit=token.daily_labels)
        try:
            self.mgr.run(self.mgr.print(img, label.width_mm, label.height_mm, lr.density, lr.copies,
                                        connect=False, source=f"API: {token.name}"), PRINT_TIMEOUT)
        except BaseException:
            self._refund_labels(token, lr.copies)
            raise
        log.info("API print by token %s (%s): %s, %d copies", token.name, token.id, lr.kind, lr.copies)
        return _json(200, {"ok": True, "printed": {"kind": lr.kind, "copies": lr.copies,
                                                   "width_mm": label.width_mm, "height_mm": label.height_mm}})

    # --- idempotency ---------------------------------------------------------
    def _idem_begin(self, token, key: str, fingerprint: str) -> Reply | None:
        now = self.clock()
        with self._lock:
            while self._idem and (len(self._idem) > IDEMPOTENCY_MAX or
                                  now - next(iter(self._idem.values())).created > IDEMPOTENCY_TTL):
                self._idem.popitem(last=False)
            entry = self._idem.get((token.id, key))
            if entry is None:
                self._idem[(token.id, key)] = _Idem(fingerprint, now)
                return None
        if entry.fingerprint != fingerprint:
            return _error(422, "idempotency_key_reused", "Idempotency-Key was used with a different request")
        if entry.reply is None:
            return _error(409, "idempotency_in_progress", "A request with this Idempotency-Key is in progress")
        return Reply(entry.reply.status, entry.reply.body, entry.reply.ctype,
                     {**entry.reply.headers, "Idempotent-Replayed": "true"})

    def _idem_finish(self, token, key: str, reply: Reply) -> None:
        with self._lock:
            entry = self._idem.get((token.id, key))
            if entry is None:
                return
            if 200 <= reply.status < 300:
                entry.reply = reply
            else:  # only successes are remembered; a failed request may be retried
                del self._idem[(token.id, key)]

    def _idem_abort(self, token, key: str) -> None:
        with self._lock:
            self._idem.pop((token.id, key), None)

    # --- errors --------------------------------------------------------------
    @staticmethod
    def _failure(e: Exception, name: str) -> Reply:
        info = err_info(e)
        if isinstance(e, RenderError):  # includes schema.ValidationError
            return _json(400, {"ok": False, "error": info})
        if isinstance(e, PrinterError) or info["code"] in ("timeout", "not_found"):
            status = 503 if info["code"] in UNAVAILABLE else 422
            log.warning("API %s failed: %s", name, e)
            return _json(status, {"ok": False, "error": info})
        log.exception("API %s failed", name)
        return _error(500, "internal", "Internal error")
