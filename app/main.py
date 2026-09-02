#!/usr/bin/env python3
"""vtests control plane: web UI, scheduler, stress-ng runner."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
VERSION_FILE = ROOT.parent / "VERSION"
COOKIE = "vtests_session"


def _version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


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


def meminfo() -> dict[str, int]:
    data: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2:
                data[parts[0].rstrip(":")] = int(parts[1])
    total = data.get("MemTotal", 0) // 1024
    avail = data.get("MemAvailable", data.get("MemFree", 0)) // 1024
    return {"total_mb": total, "avail_mb": avail, "used_mb": max(0, total - avail)}


def cpu_count() -> int:
    return os.cpu_count() or 1


def max_memory_mb(total_mb: int) -> int:
    leave = 512 if total_mb <= 2048 else max(512, int(total_mb * 0.2))
    return max(0, total_mb - leave)


def default_config() -> dict[str, Any]:
    total = meminfo()["total_mb"]
    mem_default = min(128, max_memory_mb(total)) if total else 128
    return {
        "listen": "0.0.0.0",
        "port": 8088,
        "base_path": "/" + secrets.token_urlsafe(6).rstrip("="),
        "password": secrets.token_urlsafe(12),
        "secret": secrets.token_hex(16),
        "cpu_percent": 20,
        "memory_mb": mem_default,
        "schedule_enabled": False,
        "schedule_start": "09:00",
        "schedule_end": "22:00",
        "timezone": "Asia/Shanghai",
        "enabled": False,
        "paused": False,
    }


def load_config() -> dict[str, Any]:
    path = config_path()
    cfg = default_config()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update(saved)
        except (OSError, json.JSONDecodeError):
            pass
    else:
        save_config(cfg)
    cfg["cpu_percent"] = int(max(0, min(100, int(cfg.get("cpu_percent", 20)))))
    cfg["memory_mb"] = int(max(0, int(cfg.get("memory_mb", 0))))
    cfg["port"] = int(cfg.get("port", 8088))
    base = str(cfg.get("base_path") or "/").strip() or "/"
    if not base.startswith("/"):
        base = "/" + base
    cfg["base_path"] = base.rstrip("/") or ""
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = (value or "00:00").split(":")
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


def in_window(cfg: dict[str, Any], now: datetime | None = None) -> bool:
    tz_name = cfg.get("timezone") or "Asia/Shanghai"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    current = now.astimezone(tz) if now else datetime.now(tz)
    sh, sm = parse_hhmm(str(cfg.get("schedule_start") or "09:00"))
    eh, em = parse_hhmm(str(cfg.get("schedule_end") or "22:00"))
    start = sh * 60 + sm
    end = eh * 60 + em
    cur = current.hour * 60 + current.minute
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def should_run(cfg: dict[str, Any]) -> bool:
    if cfg.get("paused"):
        return False
    if cfg.get("schedule_enabled"):
        return bool(in_window(cfg))
    return bool(cfg.get("enabled"))


class CpuSampler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prev: tuple[int, int] | None = None

    def _snap(self) -> tuple[int, int]:
        with open("/proc/stat", encoding="utf-8") as fh:
            parts = fh.readline().split()
        nums = [int(x) for x in parts[1:]]
        total = sum(nums)
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return total, idle

    def percent(self) -> float:
        with self._lock:
            cur = self._snap()
            if self._prev is None:
                self._prev = cur
                return 0.0
            dt = cur[0] - self._prev[0]
            di = cur[1] - self._prev[1]
            self._prev = cur
            if dt <= 0:
                return 0.0
            return max(0.0, min(100.0, (1.0 - di / dt) * 100.0))


class LoadEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._error = ""

    def alive(self) -> bool:
        with self._lock:
            if self._proc is None:
                return False
            return self._proc.poll() is None

    def error(self) -> str:
        return self._error

    def start(self, cfg: dict[str, Any]) -> None:
        binary = shutil.which("stress-ng")
        if not binary:
            self._error = "未找到 stress-ng，请安装后重启服务"
            return
        cpu = int(cfg.get("cpu_percent") or 0)
        mem = int(cfg.get("memory_mb") or 0)
        cap = max_memory_mb(meminfo()["total_mb"])
        if mem > cap:
            mem = cap
        cmd = [binary, "--timeout", "0"]
        if cpu > 0:
            cmd.extend(["--cpu", "0", "--cpu-load", str(cpu), "--cpu-method", "nop"])
        if mem > 0:
            cmd.extend(["--vm", "1", "--vm-bytes", f"{mem}M", "--vm-keep"])
        if cpu <= 0 and mem <= 0:
            self.stop()
            return
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._kill_locked()
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self._error = ""
            except OSError as exc:
                self._error = str(exc)
                self._proc = None

    def stop(self) -> None:
        with self._lock:
            self._kill_locked()

    def _kill_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


class Watchdog:
    def __init__(self, engine: LoadEngine) -> None:
        self.engine = engine
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_in_window: bool | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="vtests-wd", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.wait(2):
            cfg = load_config()
            window = in_window(cfg) if cfg.get("schedule_enabled") else None
            if cfg.get("schedule_enabled") and self._last_in_window is False and window:
                cfg["paused"] = False
                cfg["enabled"] = True
                save_config(cfg)
            if cfg.get("schedule_enabled") and self._last_in_window is True and window is False:
                cfg["enabled"] = False
                save_config(cfg)
            if window is not None:
                self._last_in_window = window
            want = should_run(cfg)
            alive = self.engine.alive()
            if want and not alive:
                self.engine.start(cfg)
            elif not want and alive:
                self.engine.stop()


SAMPLER = CpuSampler()
ENGINE = LoadEngine()
WATCHDOG = Watchdog(ENGINE)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    WATCHDOG.start()
    yield
    WATCHDOG.stop()
    ENGINE.stop()


app = FastAPI(title="vtests", docs_url=None, redoc_url=None, lifespan=lifespan)
if WEB_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")


def make_token(secret: str) -> str:
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"


def valid_token(token: str | None, secret: str) -> bool:
    if not token or "." not in token:
        return False
    ts, sig = token.split(".", 1)
    expect = hmac.new(secret.encode(), ts.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return False
    try:
        age = abs(time.time() - int(ts))
    except ValueError:
        return False
    return age < 7 * 24 * 3600


def authorized(request: Request) -> bool:
    cfg = load_config()
    return valid_token(request.cookies.get(COOKIE), str(cfg.get("secret") or ""))


def deny() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)


@app.middleware("http")
async def strip_prefix(request: Request, call_next):
    cfg = load_config()
    base = str(cfg.get("base_path") or "")
    path = request.scope.get("path") or "/"
    if not base:
        return await call_next(request)
    if path == base or path.startswith(base + "/"):
        request.scope["path"] = path[len(base) :] or "/"
        return await call_next(request)
    if path == "/":
        return JSONResponse({"service": "vtests", "version": _version()}, status_code=404)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/")
async def index():
    html = WEB_DIR / "index.html"
    if not html.exists():
        return JSONResponse({"error": "web ui missing"}, status_code=500)
    text = html.read_text(encoding="utf-8")
    cfg = load_config()
    text = text.replace("{{PREFIX}}", str(cfg.get("base_path") or ""))
    text = text.replace("{{VERSION}}", _version())
    return HTMLResponse(text)


@app.get("/api/status")
async def api_status(request: Request):
    if not authorized(request):
        return deny()
    cfg = load_config()
    mem = meminfo()
    loadavg = os.getloadavg()
    return {
        "ok": True,
        "version": _version(),
        "hostname": os.uname().nodename,
        "cores": cpu_count(),
        "running": ENGINE.alive(),
        "error": ENGINE.error(),
        "cpu_percent": round(SAMPLER.percent(), 1),
        "mem": mem,
        "loadavg": [round(x, 2) for x in loadavg],
        "in_window": in_window(cfg),
        "max_memory_mb": max_memory_mb(mem["total_mb"]),
        "config": public_config(cfg),
    }


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
    }


@app.post("/api/login")
async def api_login(request: Request):
    cfg = load_config()
    try:
        body = await request.json()
    except Exception:
        body = {}
    password = str(body.get("password") or "")
    if not hmac.compare_digest(password, str(cfg.get("password") or "")):
        return JSONResponse({"ok": False, "error": "密码错误"}, status_code=403)
    token = make_token(str(cfg.get("secret") or "x"))
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        path=str(cfg.get("base_path") or "/") or "/",
        max_age=7 * 24 * 3600,
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    cfg = load_config()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path=str(cfg.get("base_path") or "/") or "/")
    return resp


@app.post("/api/config")
async def api_config(request: Request):
    if not authorized(request):
        return deny()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    cfg = load_config()
    if "cpu_percent" in body:
        cfg["cpu_percent"] = int(max(0, min(100, int(body["cpu_percent"]))))
    if "memory_mb" in body:
        mem = int(max(0, int(body["memory_mb"])))
        cfg["memory_mb"] = min(mem, max_memory_mb(meminfo()["total_mb"]))
    if "schedule_enabled" in body:
        cfg["schedule_enabled"] = bool(body["schedule_enabled"])
    if "schedule_start" in body:
        cfg["schedule_start"] = str(body["schedule_start"])
    if "schedule_end" in body:
        cfg["schedule_end"] = str(body["schedule_end"])
    if "timezone" in body:
        tz = str(body["timezone"] or "Asia/Shanghai")
        try:
            ZoneInfo(tz)
            cfg["timezone"] = tz
        except Exception:
            return JSONResponse({"ok": False, "error": "时区无效"}, status_code=400)
    save_config(cfg)
    if should_run(cfg):
        ENGINE.start(cfg)
    else:
        ENGINE.stop()
    return {"ok": True, "config": public_config(cfg)}


@app.post("/api/start")
async def api_start(request: Request):
    if not authorized(request):
        return deny()
    cfg = load_config()
    cfg["enabled"] = True
    cfg["paused"] = False
    save_config(cfg)
    ENGINE.start(cfg)
    return {"ok": True, "running": ENGINE.alive(), "error": ENGINE.error()}


@app.post("/api/stop")
async def api_stop(request: Request):
    if not authorized(request):
        return deny()
    cfg = load_config()
    cfg["enabled"] = False
    cfg["paused"] = True
    save_config(cfg)
    ENGINE.stop()
    return {"ok": True, "running": False}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": _version()}


def main() -> None:
    cfg = load_config()
    uvicorn.run(
        app,
        host=str(cfg.get("listen") or "0.0.0.0"),
        port=int(cfg.get("port") or 8088),
        log_level="info",
    )


if __name__ == "__main__":
    main()
