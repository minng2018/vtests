#!/usr/bin/env python3
"""Config path, defaults, caps, atomic save, sidecar lock."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import socket
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.metrics import meminfo

ROOT = Path(__file__).resolve().parent
VALID_MODES = ("off", "manual", "schedule")
RESERVED_PORTS = frozenset({22, 80, 443, 7000, 8080})
PORT_MIN = 1024
PORT_MAX = 62000

_THREAD_LOCK = threading.RLock()


def config_path() -> Path:
    env = os.environ.get("VTESTS_CONFIG")
    if env:
        return Path(env)
    etc = Path("/etc/vtests/config.json")
    if etc.exists() or os.geteuid() == 0:
        return etc
    path = ROOT.parent / "data" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def sidecar_lock_path(path: Path | None = None) -> Path:
    target = path if path is not None else config_path()
    return target.with_name(target.name + ".lock")


def memory_hard_ceiling(total_mb: int) -> int:
    if total_mb <= 1536:
        return 128
    if total_mb <= 4096:
        return min(1024, int(total_mb * 0.40))
    return int(total_mb * 0.50)


def max_memory_mb(total_mb: int, avail_mb: int) -> int:
    reserve = max(256, int(total_mb * 0.30))
    if total_mb <= 1536:
        reserve = max(reserve, 384)
    hard_ceiling = memory_hard_ceiling(total_mb)
    from_avail = max(0, avail_mb - reserve)
    from_total = max(0, total_mb - reserve)
    return max(0, min(hard_ceiling, from_avail, from_total))


def max_cpu_percent(total_mb: int) -> int:
    if total_mb <= 1536:
        return 30
    if total_mb <= 4096:
        return 80
    return 100


def systemd_memory_max_mb(total_mb: int, avail_mb: int) -> int:
    return 100 + max_memory_mb(total_mb, avail_mb) + 64


def systemd_cpu_quota_percent(total_mb: int) -> int | None:
    if total_mb <= 1536:
        return 100
    return None


def render_systemd_limits(total_mb: int | None = None, avail_mb: int | None = None) -> str:
    if total_mb is None or avail_mb is None:
        info = meminfo()
        total_mb = info["total_mb"]
        avail_mb = info["avail_mb"]
    lines = ["[Service]", f"MemoryMax={systemd_memory_max_mb(total_mb, avail_mb)}M"]
    quota = systemd_cpu_quota_percent(total_mb)
    if quota is not None:
        lines.append(f"CPUQuota={quota}%")
    return "\n".join(lines) + "\n"


def port_is_reserved(port: int) -> bool:
    return int(port) in RESERVED_PORTS


def _port_in_use(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError:
        return True
    finally:
        sock.close()
    return False


def pick_listen_port(*, used: set[int] | None = None, check_bind: bool = False) -> int:
    env = os.environ.get("VTESTS_PORT")
    if env:
        try:
            value = int(env)
        except ValueError:
            value = 0
        if 1 <= value <= 65535:
            return value
    blocked = set(RESERVED_PORTS)
    if used:
        blocked.update(int(p) for p in used)
    for _ in range(128):
        port = secrets.randbelow(PORT_MAX - PORT_MIN + 1) + PORT_MIN
        if port in blocked:
            continue
        if check_bind and _port_in_use(port):
            continue
        return port
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port in blocked:
            continue
        if check_bind and _port_in_use(port):
            continue
        return port
    return 45123


def pick_base_path() -> str:
    env = os.environ.get("VTESTS_WEB_BASE_PATH")
    if env and env.strip():
        base = env.strip()
        if not base.startswith("/"):
            base = "/" + base
        return base.rstrip("/") or ""
    raw = secrets.token_urlsafe(9).rstrip("=")
    n = 8 + secrets.randbelow(5)
    return "/" + raw[:n]


def capped_cpu_mem(cfg: dict[str, Any], total_mb: int, avail_mb: int) -> tuple[int, int]:
    cpu = int(cfg.get("cpu_percent") or 0)
    mem = int(cfg.get("memory_mb") or 0)
    cpu = max(0, min(cpu, max_cpu_percent(total_mb), 100))
    mem = max(0, min(mem, max_memory_mb(total_mb, avail_mb)))
    return cpu, mem


def default_config() -> dict[str, Any]:
    info = meminfo()
    total = info["total_mb"]
    if total <= 1536:
        cpu_default = 10
        mem_default = 64
    elif total <= 4096:
        cpu_default = 20
        mem_default = 128
    else:
        cpu_default = 20
        mem_default = 256
    cpu_cap = max_cpu_percent(total) if total else 100
    mem_cap = memory_hard_ceiling(total) if total else mem_default
    password = os.environ.get("VTESTS_PASSWORD") or secrets.token_urlsafe(12)
    listen = os.environ.get("VTESTS_LISTEN") or "0.0.0.0"
    cfg = {
        "listen": listen,
        "port": pick_listen_port(check_bind=True),
        "base_path": pick_base_path(),
        "password": password,
        "secret": secrets.token_hex(32),
        "cpu_percent": min(cpu_default, cpu_cap),
        "memory_mb": min(mem_default, mem_cap),
        "mode": "off",
        "paused_until_next_window": False,
        "schedule_enabled": False,
        "schedule_start": "09:00",
        "schedule_end": "22:00",
        "timezone": "Asia/Shanghai",
        "enabled": False,
        "paused": False,
    }
    return cfg


def apply_legacy_migration(cfg: dict[str, Any], *, had_mode: bool | None = None) -> dict[str, Any]:
    if had_mode is None:
        had_mode = cfg.get("mode") in VALID_MODES
    if not had_mode:
        if cfg.get("schedule_enabled"):
            cfg["mode"] = "schedule"
        elif cfg.get("enabled"):
            cfg["mode"] = "manual"
        else:
            cfg["mode"] = "off"
        if cfg["mode"] == "schedule":
            cfg["paused_until_next_window"] = bool(cfg.get("paused"))
        else:
            cfg["paused_until_next_window"] = False
        return cfg
    if cfg.get("mode") not in VALID_MODES:
        cfg["mode"] = "off"
    if cfg["mode"] != "schedule":
        cfg["paused_until_next_window"] = False
    else:
        cfg["paused_until_next_window"] = bool(cfg.get("paused_until_next_window"))
    return cfg


def sync_legacy_from_mode(cfg: dict[str, Any]) -> dict[str, Any]:
    mode = cfg.get("mode") if cfg.get("mode") in VALID_MODES else "off"
    cfg["mode"] = mode
    pause = bool(cfg.get("paused_until_next_window")) if mode == "schedule" else False
    cfg["paused_until_next_window"] = pause
    if mode == "schedule":
        cfg["schedule_enabled"] = True
        cfg["enabled"] = True
        cfg["paused"] = pause
    elif mode == "manual":
        cfg["schedule_enabled"] = False
        cfg["enabled"] = True
        cfg["paused"] = False
    else:
        cfg["schedule_enabled"] = False
        cfg["enabled"] = False
        cfg["paused"] = False
    return cfg


def migrate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    had_mode = cfg.get("mode") in VALID_MODES
    apply_legacy_migration(cfg, had_mode=had_mode)
    sync_legacy_from_mode(cfg)
    return cfg


def public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "cpu_percent": int(cfg.get("cpu_percent") or 0),
        "memory_mb": int(cfg.get("memory_mb") or 0),
        "schedule_enabled": bool(cfg.get("schedule_enabled")),
        "schedule_start": cfg.get("schedule_start") or "09:00",
        "schedule_end": cfg.get("schedule_end") or "22:00",
        "timezone": cfg.get("timezone") or "Asia/Shanghai",
        "enabled": bool(cfg.get("enabled")),
        "paused": bool(cfg.get("paused")),
        "port": int(cfg.get("port") or 8088),
        "mode": cfg.get("mode") if cfg.get("mode") in VALID_MODES else "off",
        "paused_until_next_window": bool(cfg.get("paused_until_next_window")),
    }


def _coerce_fields(cfg: dict[str, Any]) -> None:
    cfg["cpu_percent"] = int(max(0, min(100, int(cfg.get("cpu_percent", 10)))))
    cfg["memory_mb"] = int(max(0, int(cfg.get("memory_mb", 0))))
    cfg["port"] = int(cfg.get("port", 8088))
    base = str(cfg.get("base_path") or "/").strip() or "/"
    if not base.startswith("/"):
        base = "/" + base
    cfg["base_path"] = base.rstrip("/") or ""
    # Persist only the MemTotal hard ceiling. Live MemAvailable is applied at engine start.
    total = meminfo()["total_mb"]
    if total:
        cfg["cpu_percent"] = min(cfg["cpu_percent"], max_cpu_percent(total))
        cfg["memory_mb"] = min(cfg["memory_mb"], memory_hard_ceiling(total))


def _prepare_for_write(cfg: dict[str, Any]) -> None:
    had_mode = cfg.get("mode") in VALID_MODES
    apply_legacy_migration(cfg, had_mode=had_mode)
    sync_legacy_from_mode(cfg)
    _coerce_fields(cfg)


def _merge_saved(defaults: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    merged.update(saved)
    # A saved file that omitted plaintext must not pick up a fresh default password.
    if "password" not in saved:
        merged.pop("password", None)
    had_mode = saved.get("mode") in VALID_MODES
    apply_legacy_migration(merged, had_mode=had_mode)
    sync_legacy_from_mode(merged)
    _coerce_fields(merged)
    return merged


def _read_or_default_unlocked() -> dict[str, Any]:
    path = config_path()
    defaults = default_config()
    if not path.exists():
        return defaults
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(saved, dict):
        return defaults
    return _merge_saved(defaults, saved)


def _atomic_write(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


@contextmanager
def _file_lock(exclusive: bool):
    path = config_path()
    lock_path = sidecar_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Sidecar inode stays put; flock on the JSON file would be lost after os.replace.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


@contextmanager
def config_lock(exclusive: bool = False):
    with _THREAD_LOCK:
        with _file_lock(exclusive):
            yield


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        cfg = default_config()
        save_config(cfg)
        return cfg
    with config_lock(exclusive=False):
        return _read_or_default_unlocked()


def save_config(cfg: dict[str, Any]) -> None:
    with config_lock(exclusive=True):
        _prepare_for_write(cfg)
        _atomic_write(config_path(), cfg)


def update_config(mutator: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    with config_lock(exclusive=True):
        cfg = _read_or_default_unlocked()
        mutator(cfg)
        _prepare_for_write(cfg)
        _atomic_write(config_path(), cfg)
        return cfg
