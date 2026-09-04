"""Password hashing, HMAC session cookies, and login rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import math
import secrets
import threading
import time
from typing import Any

from app.config import load_config, update_config

COOKIE = "vtests_session"
UID = "1"
TOKEN_TTL = 86400
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 600

SCRYPT_LN = 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_LEN = 16

_rate_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


def _now() -> float:
    return time.time()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SCRYPT_SALT_LEN)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=1 << SCRYPT_LN,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=32 * 1024 * 1024,
    )
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_LN),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def _parse_scrypt(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        ln = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = base64.b64decode(parts[4], validate=False)
        expected = base64.b64decode(parts[5], validate=False)
    except (ValueError, TypeError):
        return None
    # Bound untrusted stored params so a crafted hash cannot exhaust memory.
    if not (1 <= ln <= 20 and 1 <= r <= 32 and 1 <= p <= 32 and salt and expected):
        return None
    return ln, r, p, salt, expected


def verify_scrypt(password: str, encoded: str) -> bool:
    parsed = _parse_scrypt(encoded)
    if parsed is None:
        return False
    ln, r, p, salt, expected = parsed
    n = 1 << ln
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=max(32 * 1024 * 1024, 256 * n * r * p),
        )
    except (ValueError, TypeError, OverflowError, OSError):
        return False
    return hmac.compare_digest(digest, expected)


def verify_password(password: str, cfg: dict[str, Any]) -> tuple[bool, bool]:
    """Return (ok, should_write_hash). Hash wins when the stored string parses."""
    if not password:
        return False, False
    encoded = str(cfg.get("password_hash") or "")
    if _parse_scrypt(encoded) is not None:
        return verify_scrypt(password, encoded), False
    plaintext = cfg.get("password")
    if not isinstance(plaintext, str) or not plaintext:
        return False, False
    left = password.encode("utf-8")
    right = plaintext.encode("utf-8")
    if len(left) != len(right):
        return False, False
    ok = hmac.compare_digest(left, right)
    return ok, ok


def persist_password_hash(password: str) -> dict[str, Any]:
    hashed = hash_password(password)

    def mutate(cfg: dict[str, Any]) -> None:
        cfg["password_hash"] = hashed
        cfg.pop("password", None)

    return update_config(mutate)


def reset_password(new_password: str) -> dict[str, Any]:
    hashed = hash_password(new_password)
    new_secret = secrets.token_hex(32)

    def mutate(cfg: dict[str, Any]) -> None:
        cfg["password_hash"] = hashed
        cfg.pop("password", None)
        cfg["secret"] = new_secret

    return update_config(mutate)


def make_token(secret: str, *, now: float | None = None) -> str:
    ts = _now() if now is None else now
    exp = int(ts) + TOKEN_TTL
    payload = f"{UID}.{exp}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_token(token: str | None, secret: str, *, now: float | None = None) -> bool:
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    uid, exp_s, sig = parts
    if uid != UID or len(sig) != 64:
        return False
    payload = f"{uid}.{exp_s}"
    expect = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if len(expect) != 64 or not hmac.compare_digest(sig, expect):
        return False
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    ts = _now() if now is None else now
    return ts < exp


def authorized(request: Any) -> bool:
    cfg = load_config()
    cookies = getattr(request, "cookies", {}) or {}
    token = cookies.get(COOKIE) if hasattr(cookies, "get") else None
    return valid_token(token, str(cfg.get("secret") or ""))


def apply_session_cookie(response: Any, token: str, cfg: dict[str, Any]) -> None:
    path = str(cfg.get("base_path") or "/") or "/"
    # TLS terminates at nginx; Secure must follow ssl_enabled, not the request scheme.
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        path=path,
        max_age=TOKEN_TTL,
        secure=bool(cfg.get("ssl_enabled")),
    )


def normalize_client_ip(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    if text.count(":") == 1 and "." in text:
        host, maybe_port = text.rsplit(":", 1)
        if maybe_port.isdigit():
            text = host
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return str(mapped)
    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr)
    return addr.exploded


def _header(request: Any, name: str) -> str:
    headers = getattr(request, "headers", None) or {}
    want = name.lower()
    if hasattr(headers, "items"):
        for key, val in headers.items():
            if str(key).lower() == want:
                return str(val or "").strip()
    return ""


def _peer_host(request: Any) -> str:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    if not host:
        return "unknown"
    return normalize_client_ip(str(host)) or str(host)


def _trust_forwarded(cfg: dict[str, Any]) -> bool:
    if cfg.get("ssl_enabled"):
        return True
    listen = str(cfg.get("listen") or "0.0.0.0").strip()
    if listen in ("0.0.0.0", "::"):
        return False
    try:
        return ipaddress.ip_address(listen).is_loopback
    except ValueError:
        return False


def client_rate_key(request: Any, cfg: dict[str, Any]) -> str:
    if _trust_forwarded(cfg):
        real = _header(request, "x-real-ip")
        if real:
            ip = normalize_client_ip(real.split(",", 1)[0])
            if ip:
                return ip
        xff = _header(request, "x-forwarded-for")
        if xff:
            ip = normalize_client_ip(xff.split(",", 1)[0])
            if ip:
                return ip
    return _peer_host(request)


def rate_limit_retry_after(key: str) -> int | None:
    now = _now()
    cutoff = now - RATE_LIMIT_WINDOW
    with _rate_lock:
        times = sorted(t for t in _failures.get(key, ()) if t > cutoff)
        if times:
            _failures[key] = times
        else:
            _failures.pop(key, None)
        if len(times) >= RATE_LIMIT_MAX:
            retry = int(math.ceil(times[0] + RATE_LIMIT_WINDOW - now))
            return max(1, retry)
    return None


def record_login_failure(key: str) -> None:
    now = _now()
    cutoff = now - RATE_LIMIT_WINDOW
    with _rate_lock:
        times = [t for t in _failures.get(key, ()) if t > cutoff]
        times.append(now)
        _failures[key] = times


def record_login_success(key: str) -> None:
    with _rate_lock:
        _failures.pop(key, None)


def reset_rate_limits() -> None:
    with _rate_lock:
        _failures.clear()
