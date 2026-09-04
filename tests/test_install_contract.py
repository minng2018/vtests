from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.main import _assert_runtime

ROOT = Path(__file__).resolve().parent.parent


def test_unit_non_root_no_hardcoded_memorymax():
    text = (ROOT / "systemd/vtests.service").read_text(encoding="utf-8")
    assert "User=vtests" in text
    assert "Group=vtests" in text
    assert "WorkingDirectory=/var/lib/vtests" in text
    assert "MemoryMax=240" not in text
    assert "MemoryMax=240M" not in text
    assert "单进程" in text
    assert "gunicorn" in text


def test_install_sh_contract():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "useradd --system --home" in text
    assert "--shell /usr/sbin/nologin vtests" in text
    assert "tzdata" in text
    assert "VTESTS_OPEN_FIREWALL" in text
    assert "iptables -I" not in text
    assert "iptables -C" not in text
    assert "seq 1 40" in text
    assert "sleep 0.5" in text
    assert "hash_password" in text
    assert "sudo -u vtests" in text
    assert "vtests:vtests 600" in text
    assert 'cp -a "${src}/nginx"' in text or "nginx" in text
    assert "cp \"${src}/install.sh\"" in text or 'cp "${src}/install.sh"' in text


def test_vtests_sh_cli_contract():
    text = (ROOT / "vtests.sh").read_text(encoding="utf-8")
    assert "/opt/vtests/install.sh" in text
    assert "启动/停止 = 面板服务" in text
    assert "sudo -u vtests" in text
    assert "reset_password" in text
    assert 'password)' in text
    assert "do_uninstall" in text


def test_uvicorn_workers_gt1_exits(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        _assert_runtime()


def test_uvicorn_workers_1_ok(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    _assert_runtime()
