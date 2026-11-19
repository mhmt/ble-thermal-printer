"""API tokens for /api/v1 (managed with `ble-thermal-printer token ...`).

Only a SHA-256 hash of each token is stored in the config file; the token itself is shown once
when it is created. Tokens are 256-bit random values, so a fast hash is sufficient.
The server reads the list on every request, so creating or revoking a token takes effect
without a restart.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from . import config

PREFIX = "btp_"
SCOPES = ("print", "read")       # print: print + read; read: status and preview only
DEFAULT_DAILY_LABELS = 200       # labels (copies) per token per day
DEFAULT_RATE_PER_MIN = 30        # API requests per token per minute
MAX_NAME = 40


class TokenError(ValueError):
    def __init__(self, code: str, message: str, **params):
        super().__init__(message)
        self.code = code
        self.params = params


@dataclass(frozen=True)
class Token:
    id: str
    name: str
    scope: str
    daily_labels: int
    rate_per_min: int
    created: float

    def allows(self, scope: str) -> bool:
        return self.scope == "print" or scope == "read"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _entries(cfg: dict) -> list[dict]:
    entries = cfg.get("api_tokens")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _to_token(e: dict) -> Token:
    return Token(id=str(e.get("id", "")), name=str(e.get("name", "")), scope=e.get("scope", "read"),
                 daily_labels=int(e.get("daily_labels", DEFAULT_DAILY_LABELS)),
                 rate_per_min=int(e.get("rate_per_min", DEFAULT_RATE_PER_MIN)),
                 created=float(e.get("created", 0)))


def list_tokens() -> list[Token]:
    return [_to_token(e) for e in _entries(config.load())]


def create(name: str, scope: str = "print", daily_labels: int = DEFAULT_DAILY_LABELS,
           rate_per_min: int = DEFAULT_RATE_PER_MIN) -> tuple[Token, str]:
    """Add a token; returns it with the secret value, which is not stored anywhere."""
    name = name.strip()
    if not name or len(name) > MAX_NAME:
        raise TokenError("token_bad_name", f"Name must be 1-{MAX_NAME} characters", max=MAX_NAME)
    if scope not in SCOPES:
        raise TokenError("token_bad_scope", f"Scope must be one of {', '.join(SCOPES)}")
    if daily_labels < 0 or rate_per_min < 1:
        raise TokenError("token_bad_limit", "Limits must be positive")
    cfg = config.load()
    entries = _entries(cfg)
    if any(e.get("name") == name for e in entries):
        raise TokenError("token_exists", f"A token named '{name}' already exists", name=name)
    secret = PREFIX + secrets.token_urlsafe(32)
    entry = {"id": secrets.token_hex(4), "name": name, "scope": scope, "hash": _hash(secret),
             "daily_labels": daily_labels, "rate_per_min": rate_per_min, "created": round(time.time())}
    cfg["api_tokens"] = [*entries, entry]
    config.save(cfg)
    return _to_token(entry), secret


def revoke(id_or_name: str) -> Token:
    cfg = config.load()
    entries = _entries(cfg)
    keep = [e for e in entries if id_or_name not in (e.get("id"), e.get("name"))]
    if len(keep) == len(entries):
        raise TokenError("token_not_found", f"No token with id or name '{id_or_name}'", token=id_or_name)
    removed = next(e for e in entries if e not in keep)
    cfg["api_tokens"] = keep
    config.save(cfg)
    return _to_token(removed)


def verify(secret: str) -> Token | None:
    """The token matching this secret, or None. Compares every entry in constant time."""
    if not secret.startswith(PREFIX):
        return None
    digest = _hash(secret)
    found = None
    for e in _entries(config.load()):
        if hmac.compare_digest(str(e.get("hash", "")), digest):
            found = e
    return _to_token(found) if found else None
