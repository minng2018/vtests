from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import load_config, save_config
from app.main import app


def _host_info():
    return {"total_mb": 954, "avail_mb": 550, "used_mb": 404}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VTESTS_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr("app.config.meminfo", _host_info)
    monkeypatch.setattr("app.metrics.meminfo", _host_info)
    monkeypatch.setattr("app.main.meminfo", _host_info)
    monkeypatch.setattr("app.main.WATCHDOG.start", lambda: None)
    monkeypatch.setattr("app.main.WATCHDOG.stop", lambda: None)
    monkeypatch.setattr("app.main.ENGINE.start", lambda cfg: None)
    monkeypatch.setattr("app.main.ENGINE.stop", lambda: None)
    monkeypatch.setattr("app.main.ENGINE.alive", lambda: False)
    monkeypatch.setattr("app.main.ENGINE.error", lambda: "")
    auth.reset_rate_limits()
    yield tmp_path
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


def _login(client: TestClient) -> None:
    resp = client.post("/api/login", json={"password": "s3cret"})
    assert resp.status_code == 200


def test_unmatched_root_does_not_leak_service_name():
    _cfg(base_path="/xK92abQ1")
    with _client() as client:
        resp = client.get("/")
        assert resp.status_code == 404
        body = resp.json()
        dumped = json.dumps(body).lower()
        assert "vtests" not in dumped
        assert "service" not in body
        assert "version" not in body
        assert body == {"error": "not found"}

        other = client.get("/nope")
        assert other.status_code == 404
        assert "vtests" not in json.dumps(other.json()).lower()
        assert other.json() == {"error": "not found"}


def test_empty_base_path_serves_ui():
    _cfg(base_path="/")
    with _client() as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "面板密码" in resp.text
        assert 'id="mode"' in resp.text


def test_prefixed_root_serves_login_and_settings_controls():
    _cfg(base_path="/xK92abQ1")
    with _client() as client:
        resp = client.get("/xK92abQ1/")
        assert resp.status_code == 200
        html = resp.text
        assert "面板密码" in html
        assert 'id="loginBtn"' in html
        assert 'id="startBtn"' in html
        assert 'id="stopBtn"' in html
        assert 'id="mode"' in html
        assert 'value="off"' in html
        assert 'value="manual"' in html
        assert 'value="schedule"' in html
        assert "已暂停到下一时间段" in html
        assert "max_cpu_percent" in html
        assert "max_memory_mb" in html
        assert "可能影响本机其它服务" in html
        assert "/api/start" in html
        assert "/api/stop" in html
        assert 'mode: $("mode").value' in html


def test_status_exposes_caps_and_mode():
    _cfg()
    with _client() as client:
        _login(client)
        status = client.get("/api/status")
        assert status.status_code == 200
        data = status.json()
        assert data["max_cpu_percent"] == 30
        assert data["max_memory_mb"] == 128
        assert data["config"]["mode"] == "off"
        assert data["config"]["paused_until_next_window"] is False
        assert data["config"]["cpu_percent"] == 10
        assert data["config"]["memory_mb"] == 64


def test_config_posts_mode_and_clamps_cpu():
    _cfg()
    with _client() as client:
        _login(client)
        saved = client.post(
            "/api/config",
            json={
                "cpu_percent": 100,
                "memory_mb": 64,
                "mode": "schedule",
                "schedule_start": "09:00",
                "schedule_end": "22:00",
                "timezone": "UTC",
            },
        )
        assert saved.status_code == 200
        cfg = saved.json()["config"]
        assert cfg["cpu_percent"] == 30
        assert cfg["mode"] == "schedule"
        assert cfg["schedule_enabled"] is True
        assert cfg["paused_until_next_window"] is False

        status = client.get("/api/status")
        assert status.json()["config"]["mode"] == "schedule"
        assert status.json()["config"]["cpu_percent"] == 30


def test_config_rejects_invalid_mode():
    _cfg()
    with _client() as client:
        _login(client)
        resp = client.post("/api/config", json={"mode": "burst"})
        assert resp.status_code == 400
        assert load_config()["mode"] == "off"
