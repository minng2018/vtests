from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import load_config, save_config, update_config
from app.main import app


def _host_info():
    return {"total_mb": 954, "avail_mb": 550, "used_mb": 404}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    monkeypatch.setattr("app.config.meminfo", _host_info)
    monkeypatch.setattr("app.metrics.meminfo", _host_info)
    monkeypatch.setattr("app.main.WATCHDOG.start", lambda: None)
    monkeypatch.setattr("app.main.WATCHDOG.stop", lambda: None)
    auth.reset_rate_limits()
    yield cfg_path
    auth.reset_rate_limits()


def _cfg(**kwargs):
    cfg = {
        "listen": "0.0.0.0",
        "port": 8088,
        "base_path": "/",
        "password": "s3cret",
        "secret": "aabbccddeeff0011",
        "cpu_percent": 10,
        "memory_mb": 64,
        "mode": "off",
        "paused_until_next_window": False,
        "schedule_start": "09:00",
        "schedule_end": "22:00",
        "timezone": "UTC",
        "ssl_enabled": False,
    }
    cfg.update(kwargs)
    save_config(cfg)
    return load_config()


def _client():
    return TestClient(app)


def _req(host="192.0.2.10", headers=None):
    return SimpleNamespace(headers=headers or {}, client=SimpleNamespace(host=host))


def test_scrypt_round_trip():
    encoded = auth.hash_password("correct horse")
    parts = encoded.split("$")
    assert parts[0] == "scrypt"
    assert parts[1] == "14"
    assert parts[2] == "8"
    assert parts[3] == "1"
    assert auth.verify_scrypt("correct horse", encoded)
    assert not auth.verify_scrypt("wrong password", encoded)


def test_scrypt_wrong_password_fails():
    encoded = auth.hash_password("pw")
    assert not auth.verify_scrypt("PW", encoded)
    assert not auth.verify_scrypt("", encoded)


def test_plaintext_fallback_writes_hash(tmp_path):
    _cfg(password="s3cret")
    with _client() as client:
        resp = client.post("/api/login", json={"password": "s3cret"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    cookie = resp.headers.get("set-cookie", "")
    assert "Max-Age=86400" in cookie
    assert "HttpOnly" in cookie
    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "password" not in raw
    assert raw["password_hash"].startswith("scrypt$14$8$1$")
    assert auth.verify_scrypt("s3cret", raw["password_hash"])
    loaded = load_config()
    assert "password" not in loaded
    update_config(lambda cfg: cfg.__setitem__("timezone", "UTC"))
    after = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "password" not in after
    assert after["password_hash"] == raw["password_hash"]


def test_hmac_full_hex_and_truncated_rejected():
    secret = "super-secret"
    token = auth.make_token(secret)
    uid, exp, sig = token.split(".")
    assert uid == "1"
    assert len(sig) == 64
    assert int(exp) > 0
    assert auth.valid_token(token, secret)
    assert not auth.valid_token(f"{uid}.{exp}.{sig[:32]}", secret)
    assert not auth.valid_token(f"{exp}.{sig[:32]}", secret)


def test_expired_token_rejected():
    secret = "super-secret"
    token = auth.make_token(secret, now=1_000_000.0)
    assert not auth.valid_token(token, secret, now=1_000_000.0 + auth.TOKEN_TTL)
    assert auth.valid_token(token, secret, now=1_000_000.0 + 10)


def test_login_rate_limit_429(monkeypatch):
    clock = {"t": 2_000_000.0}
    monkeypatch.setattr(auth, "_now", lambda: clock["t"])
    _cfg(password="s3cret")
    with _client() as client:
        for _ in range(5):
            resp = client.post("/api/login", json={"password": "nope"})
            assert resp.status_code == 403
        blocked = client.post("/api/login", json={"password": "s3cret"})
        assert blocked.status_code == 429
        assert blocked.headers.get("retry-after")
        clock["t"] += auth.RATE_LIMIT_WINDOW + 1
        ok = client.post("/api/login", json={"password": "s3cret"})
        assert ok.status_code == 200


def test_public_bind_ignores_x_forwarded_for():
    _cfg(listen="0.0.0.0", ssl_enabled=False, password="s3cret")
    with _client() as client:
        for _ in range(5):
            resp = client.post(
                "/api/login",
                json={"password": "nope"},
                headers={"X-Forwarded-For": "198.51.100.1"},
            )
            assert resp.status_code == 403
        blocked = client.post(
            "/api/login",
            json={"password": "s3cret"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert blocked.status_code == 429


def test_loopback_uses_forwarded_for():
    _cfg(listen="127.0.0.1", ssl_enabled=False, password="s3cret")
    with _client() as client:
        for _ in range(5):
            resp = client.post(
                "/api/login",
                json={"password": "nope"},
                headers={"X-Forwarded-For": "198.51.100.1"},
            )
            assert resp.status_code == 403
        other = client.post(
            "/api/login",
            json={"password": "nope"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert other.status_code == 403
        blocked = client.post(
            "/api/login",
            json={"password": "s3cret"},
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        assert blocked.status_code == 429


def test_hashed_login_and_truncated_cookie_rejected():
    hashed = auth.hash_password("s3cret")
    cfg = _cfg(password_hash=hashed)
    cfg.pop("password", None)
    save_config(cfg)
    with _client() as client:
        bad = client.post("/api/login", json={"password": "nope"})
        assert bad.status_code == 403
        ok = client.post("/api/login", json={"password": "s3cret"})
        assert ok.status_code == 200
        status = client.get("/api/status")
        assert status.status_code == 200
        token = client.cookies.get(auth.COOKIE)
        assert token
        uid, exp, sig = token.split(".")
        truncated = f"{uid}.{exp}.{sig[:32]}"
    with _client() as client:
        client.cookies.set(auth.COOKIE, truncated)
        denied = client.get("/api/status")
        assert denied.status_code == 401


def test_normalize_client_ip():
    assert auth.normalize_client_ip("192.0.2.1") == "192.0.2.1"
    assert auth.normalize_client_ip("::ffff:192.0.2.1") == "192.0.2.1"
    assert auth.normalize_client_ip("2001:db8::1") == "2001:0db8:0000:0000:0000:0000:0000:0001"


def test_public_bind_key_is_peer_not_xff():
    cfg = {"listen": "0.0.0.0", "ssl_enabled": False}
    req = _req(host="192.0.2.10", headers={"X-Forwarded-For": "198.51.100.1"})
    assert auth.client_rate_key(req, cfg) == "192.0.2.10"
    loop = {"listen": "127.0.0.1", "ssl_enabled": False}
    assert auth.client_rate_key(req, loop) == "198.51.100.1"
    ssl = {"listen": "0.0.0.0", "ssl_enabled": True}
    req_real = _req(
        host="192.0.2.10",
        headers={"X-Real-IP": "203.0.113.5", "X-Forwarded-For": "198.51.100.1"},
    )
    assert auth.client_rate_key(req_real, ssl) == "203.0.113.5"


def test_reset_password_rotates_secret():
    _cfg(password="old", secret="oldsecretoldsecret")
    token = auth.make_token("oldsecretoldsecret")
    assert auth.valid_token(token, "oldsecretoldsecret")
    cfg = auth.reset_password("new-pass")
    assert cfg["password_hash"].startswith("scrypt$")
    assert "password" not in cfg
    assert cfg["secret"] != "oldsecretoldsecret"
    assert len(cfg["secret"]) == 64
    assert not auth.valid_token(token, cfg["secret"])
    assert auth.verify_scrypt("new-pass", cfg["password_hash"])
