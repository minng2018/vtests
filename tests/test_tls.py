from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import default_config, load_config, public_config, save_config
from app.main import app

ROOT = Path(__file__).resolve().parent.parent


def test_tls_uninstall_shell_script():
    script = ROOT / "tests" / "test_tls_uninstall.sh"
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"test_tls_uninstall.sh failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    assert "ALL TLS TESTS PASSED" in proc.stdout


def test_install_sh_tls_contract():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "certbot certonly --webroot" in text
    assert "setup_tls" in text
    assert "tls_fallback" in text
    assert "enable_tls" in text
    assert "VTESTS_TLS_DRY_RUN" in text
    assert "--no-upgrade" in text
    assert "managed-by: vtests" in text or "managed-by: vtests" in (
        ROOT / "nginx/vtests.conf.template"
    ).read_text(encoding="utf-8")
    assert "--resolve" in text
    assert "cfg_get ssl_enabled" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.search(r"certbot\s+--nginx\b", stripped)
        assert not re.search(r"certonly\s+--nginx\b", stripped)
        assert " --expand" not in stripped
        assert not re.search(r"\b--expand\b", stripped)
        assert "systemctl stop nginx" not in stripped
        assert "apt remove nginx" not in stripped
        assert "rm -rf /etc/letsencrypt" not in stripped
    assert "python3-certbot-nginx" not in text or "不安装" in text or "plugin" in text.lower()


def test_nginx_templates_are_owned_vhosts():
    http = (ROOT / "nginx/vtests.conf.template").read_text(encoding="utf-8")
    ssl = (ROOT / "nginx/vtests-ssl.conf.template").read_text(encoding="utf-8")
    for text in (http, ssl):
        assert "managed-by: vtests" in text
        assert "server_name __DOMAIN__;" in text
        assert "default_server" not in text
        assert "proxy_pass http://127.0.0.1:__PANEL_PORT__;" in text
    assert "listen 443 ssl;" in ssl
    assert "ssl_certificate" in ssl
    assert "return 301 https://$host$request_uri;" in ssl


def test_vtests_status_and_port_tls_contract():
    text = (ROOT / "vtests.sh").read_text(encoding="utf-8")
    assert "本机备用" in text
    assert "proxy_pass" in text
    assert "SSL_ENABLED" in text
    assert "managed-by: vtests" in text


def test_web_ui_shows_domain_readonly():
    html = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
    assert "tlsLine" in html
    assert "HTTPS 已启用（只读）" in html
    assert "未绑定域名" in html


def _host_info():
    return {"total_mb": 954, "avail_mb": 550, "used_mb": 404}


def test_public_config_exposes_tls_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("VTESTS_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr("app.config.meminfo", _host_info)
    save_config(
        {
            "listen": "127.0.0.1",
            "port": 45123,
            "base_path": "/abc",
            "password": "pw",
            "secret": "aa",
            "cpu_percent": 10,
            "memory_mb": 64,
            "mode": "off",
            "schedule_start": "09:00",
            "schedule_end": "22:00",
            "timezone": "UTC",
            "domain": "vt-frp.beeorbit.net",
            "ssl_enabled": True,
        }
    )
    pub = public_config(load_config())
    assert pub["domain"] == "vt-frp.beeorbit.net"
    assert pub["ssl_enabled"] is True
    assert pub["listen"] == "127.0.0.1"


def test_default_config_tls_off(monkeypatch):
    monkeypatch.setattr("app.config.meminfo", _host_info)
    monkeypatch.delenv("VTESTS_PORT", raising=False)
    cfg = default_config()
    assert cfg["ssl_enabled"] is False
    assert cfg["domain"] == ""
    assert cfg["listen"] == "0.0.0.0"


def test_api_config_ignores_tls_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("VTESTS_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr("app.config.meminfo", _host_info)
    monkeypatch.setattr("app.metrics.meminfo", _host_info)
    monkeypatch.setattr("app.main.WATCHDOG.start", lambda: None)
    monkeypatch.setattr("app.main.WATCHDOG.stop", lambda: None)
    save_config(
        {
            "listen": "0.0.0.0",
            "port": 8088,
            "base_path": "/",
            "password": "s3cret",
            "secret": "aabbccddeeff0011",
            "cpu_percent": 10,
            "memory_mb": 64,
            "mode": "off",
            "schedule_start": "09:00",
            "schedule_end": "22:00",
            "timezone": "UTC",
            "domain": "",
            "ssl_enabled": False,
        }
    )
    with TestClient(app) as client:
        assert client.post("/api/login", json={"password": "s3cret"}).status_code == 200
        resp = client.post(
            "/api/config",
            json={
                "cpu_percent": 10,
                "domain": "evil.example",
                "ssl_enabled": True,
                "listen": "1.2.3.4",
                "port": 443,
                "cert_path": "/tmp/x",
                "key_path": "/tmp/y",
            },
        )
        assert resp.status_code == 200
        cfg = resp.json()["config"]
        assert cfg["domain"] == ""
        assert cfg["ssl_enabled"] is False
        assert cfg["port"] == 8088
        raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert raw.get("domain") == ""
        assert raw.get("ssl_enabled") is False
        assert raw.get("listen") == "0.0.0.0"


def test_ssl_enabled_sets_secure_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("VTESTS_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr("app.config.meminfo", _host_info)
    monkeypatch.setattr("app.metrics.meminfo", _host_info)
    monkeypatch.setattr("app.main.WATCHDOG.start", lambda: None)
    monkeypatch.setattr("app.main.WATCHDOG.stop", lambda: None)
    save_config(
        {
            "listen": "127.0.0.1",
            "port": 8088,
            "base_path": "/",
            "password": "s3cret",
            "secret": "aabbccddeeff0011",
            "cpu_percent": 10,
            "memory_mb": 64,
            "mode": "off",
            "schedule_start": "09:00",
            "schedule_end": "22:00",
            "timezone": "UTC",
            "ssl_enabled": True,
            "domain": "vt-frp.beeorbit.net",
        }
    )
    with TestClient(app) as client:
        resp = client.post("/api/login", json={"password": "s3cret"})
    assert resp.status_code == 200
    cookie = resp.headers.get("set-cookie", "")
    assert "Secure" in cookie or "secure" in cookie.lower()
