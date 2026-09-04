from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app.main import _assert_runtime

ROOT = Path(__file__).resolve().parent.parent


def test_unit_non_root_no_hardcoded_memorymax():
    text = (ROOT / "systemd/vtests.service").read_text(encoding="utf-8")
    assert "User=vtests" in text
    assert "Group=vtests" in text
    assert "WorkingDirectory=/var/lib/vtests" in text
    assert "StateDirectoryMode=0750" in text
    assert "LogsDirectoryMode=0750" in text
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
    assert "certbot certonly --webroot" in text
    assert "enable_tls" in text
    assert "tls_fallback" in text
    assert "VTESTS_DOMAIN" in text
    assert "VTESTS_TLS_DRY_RUN" in text


def test_vtests_sh_cli_contract():
    text = (ROOT / "vtests.sh").read_text(encoding="utf-8")
    assert "/opt/vtests/install.sh" in text
    assert "启动/停止 = 面板服务" in text
    assert "sudo -u vtests" in text
    assert "reset_password" in text
    assert 'password)' in text
    assert "do_uninstall" in text
    assert "本机备用" in text
    assert "proxy_pass" in text


def test_uvicorn_workers_gt1_exits(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        _assert_runtime()


def test_uvicorn_workers_1_ok(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    _assert_runtime()


def test_requirements_lock_pins_runtime_and_pytest():
    path = ROOT / "requirements.lock"
    assert path.is_file()
    pinned: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, ver = line.split("==", 1)
        pinned[name.lower()] = ver
    for pkg in ("fastapi", "uvicorn", "pytest", "httpx2"):
        assert pkg in pinned, f"{pkg} missing from requirements.lock"
        assert pinned[pkg], f"{pkg} has empty version"
        assert ">=" not in pinned[pkg]
        assert "*" not in pinned[pkg]


def test_ci_workflow_runs_unit_and_tls_without_ssh_or_le():
    path = ROOT / ".github/workflows/ci.yml"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "ubuntu-24.04" in text
    assert "requirements.lock" in text
    assert "pytest" in text
    assert "test_tls_uninstall.sh" in text
    assert "VTESTS_TLS_DRY_RUN" in text
    assert 'VTESTS_DOMAIN: ""' in text
    assert "158.101.29.241" not in text
    assert "vt-frp.beeorbit.net" not in text
    assert "letsencrypt.org" not in text
    assert "acme-v02" not in text
    assert "certbot certonly" not in text
    assert not re.search(r"(?i)\b(ssh|scp|rsync)\b", text)
