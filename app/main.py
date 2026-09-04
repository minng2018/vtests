#!/usr/bin/env python3
"""vtests control plane: web UI, scheduler, stress-ng runner."""

from __future__ import annotations

import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.auth import (
    COOKIE,
    apply_session_cookie,
    authorized,
    client_rate_key,
    make_token,
    persist_password_hash,
    rate_limit_retry_after,
    record_login_failure,
    record_login_success,
    verify_password,
)
from app.config import (
    VALID_MODES,
    load_config,
    max_cpu_percent,
    max_memory_mb,
    public_config,
    save_config,
    sync_legacy_from_mode,
    update_config,
)
from app.engine import LoadEngine
from app.metrics import CpuSampler, cpu_count, meminfo
from app.scheduler import apply_start, apply_stop, in_window, normalize_hhmm, should_run

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
VERSION_FILE = ROOT.parent / "VERSION"


def _version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


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
            window = in_window(cfg)
            if (
                cfg.get("mode") == "schedule"
                and self._last_in_window is False
                and window
                and cfg.get("paused_until_next_window")
            ):
                def _clear_pause(cur: dict[str, Any]) -> None:
                    if cur.get("mode") == "schedule" and cur.get("paused_until_next_window"):
                        cur["paused_until_next_window"] = False
                        sync_legacy_from_mode(cur)

                cfg = update_config(_clear_pause)
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
        "max_memory_mb": max_memory_mb(mem["total_mb"], mem["avail_mb"]),
        "max_cpu_percent": max_cpu_percent(mem["total_mb"]),
        "config": public_config(cfg),
    }


@app.post("/api/login")
async def api_login(request: Request):
    cfg = load_config()
    key = client_rate_key(request, cfg)
    retry_after = rate_limit_retry_after(key)
    if retry_after is not None:
        return JSONResponse(
            {"ok": False, "error": "尝试次数过多"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    password = str(body.get("password") or "")
    ok, migrate = verify_password(password, cfg)
    if not ok:
        record_login_failure(key)
        return JSONResponse({"ok": False, "error": "密码错误"}, status_code=403)
    record_login_success(key)
    if migrate:
        cfg = persist_password_hash(password)
    token = make_token(str(cfg.get("secret") or ""))
    resp = JSONResponse({"ok": True})
    apply_session_cookie(resp, token, cfg)
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

    if "cpu_percent" in body:
        try:
            int(body["cpu_percent"])
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "cpu_percent 无效"}, status_code=400)
    if "memory_mb" in body:
        try:
            int(body["memory_mb"])
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "memory_mb 无效"}, status_code=400)
    if "schedule_start" in body:
        if normalize_hhmm(str(body["schedule_start"])) is None:
            return JSONResponse({"ok": False, "error": "开始时间无效"}, status_code=400)
    if "schedule_end" in body:
        if normalize_hhmm(str(body["schedule_end"])) is None:
            return JSONResponse({"ok": False, "error": "结束时间无效"}, status_code=400)
    if "timezone" in body:
        tz = str(body["timezone"] or "Asia/Shanghai")
        try:
            ZoneInfo(tz)
        except Exception:
            return JSONResponse({"ok": False, "error": "时区无效"}, status_code=400)
    if "mode" in body and str(body["mode"]) not in VALID_MODES:
        return JSONResponse({"ok": False, "error": "mode 无效"}, status_code=400)

    def mutate(cfg: dict[str, Any]) -> None:
        if "cpu_percent" in body:
            cfg["cpu_percent"] = int(max(0, min(100, int(body["cpu_percent"]))))
        if "memory_mb" in body:
            cfg["memory_mb"] = int(max(0, int(body["memory_mb"])))
        if "schedule_start" in body:
            cfg["schedule_start"] = normalize_hhmm(str(body["schedule_start"])) or str(
                body["schedule_start"]
            )
        if "schedule_end" in body:
            cfg["schedule_end"] = normalize_hhmm(str(body["schedule_end"])) or str(body["schedule_end"])
        if "timezone" in body:
            cfg["timezone"] = str(body["timezone"] or "Asia/Shanghai")
        if "mode" in body:
            mode = str(body["mode"])
            cfg["mode"] = mode
            if mode != "schedule":
                cfg["paused_until_next_window"] = False
        elif "schedule_enabled" in body:
            if body["schedule_enabled"]:
                if cfg.get("mode") != "schedule":
                    cfg["paused_until_next_window"] = False
                cfg["mode"] = "schedule"
            elif cfg.get("mode") == "schedule":
                cfg["mode"] = "off"
                cfg["paused_until_next_window"] = False
        if "paused_until_next_window" in body:
            cfg["paused_until_next_window"] = bool(body["paused_until_next_window"])
        sync_legacy_from_mode(cfg)

    cfg = update_config(mutate)
    if should_run(cfg):
        ENGINE.start(cfg)
    else:
        ENGINE.stop()
    return {"ok": True, "config": public_config(cfg)}


@app.post("/api/start")
async def api_start(request: Request):
    if not authorized(request):
        return deny()
    cfg = update_config(apply_start)
    ENGINE.start(cfg)
    return {"ok": True, "running": ENGINE.alive(), "error": ENGINE.error()}


@app.post("/api/stop")
async def api_stop(request: Request):
    if not authorized(request):
        return deny()
    update_config(apply_stop)
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
