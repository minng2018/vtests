#!/usr/bin/env python3
"""Daily window checks and mode state transitions."""

from __future__ import annotations

import re
import threading
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import (
    VALID_MODES,
    apply_legacy_migration,
    load_config,
    sync_legacy_from_mode,
    update_config,
)

HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(?:\:[0-5]\d)?$")


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = (value or "00:00").split(":")
    hour = int(parts[0]) if parts and parts[0] else 0
    minute = int(parts[1]) if len(parts) > 1 else 0
    return hour, minute


def valid_hhmm(value: str) -> bool:
    return bool(HHMM_RE.match(value or ""))


def normalize_hhmm(value: str) -> str | None:
    text = (value or "").strip()
    if not valid_hhmm(text):
        return None
    return text[:5]


def _cfg_tz(cfg: dict[str, Any]) -> ZoneInfo:
    tz_name = cfg.get("timezone") or "Asia/Shanghai"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def _local_dt(cfg: dict[str, Any], now: datetime | None = None) -> datetime:
    tz = _cfg_tz(cfg)
    return now.astimezone(tz) if now else datetime.now(tz)


def _window_minutes(cfg: dict[str, Any]) -> tuple[int, int]:
    sh, sm = parse_hhmm(str(cfg.get("schedule_start") or "09:00"))
    eh, em = parse_hhmm(str(cfg.get("schedule_end") or "22:00"))
    return sh * 60 + sm, eh * 60 + em


def in_window(cfg: dict[str, Any], now: datetime | None = None) -> bool:
    current = _local_dt(cfg, now)
    start, end = _window_minutes(cfg)
    cur = current.hour * 60 + current.minute
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def should_run(cfg: dict[str, Any], now: datetime | None = None) -> bool:
    mode = cfg.get("mode") if cfg.get("mode") in VALID_MODES else "off"
    if mode == "off":
        return False
    if cfg.get("paused_until_next_window"):
        return False
    if mode == "manual":
        return True
    if mode == "schedule":
        return bool(in_window(cfg, now))
    return False


def apply_start(cfg: dict[str, Any]) -> dict[str, Any]:
    apply_legacy_migration(cfg)
    if cfg.get("mode") == "schedule":
        cfg["paused_until_next_window"] = False
    else:
        cfg["mode"] = "manual"
        cfg["paused_until_next_window"] = False
    sync_legacy_from_mode(cfg)
    return cfg


def apply_stop(cfg: dict[str, Any]) -> dict[str, Any]:
    apply_legacy_migration(cfg)
    if cfg.get("mode") == "schedule":
        cfg["paused_until_next_window"] = True
    else:
        cfg["mode"] = "off"
        cfg["paused_until_next_window"] = False
    sync_legacy_from_mode(cfg)
    return cfg


def clear_pause_on_rising_edge(
    cfg: dict[str, Any],
    last_in_window: bool | None,
    now: datetime | None = None,
    *,
    last_local_date: date | None = None,
) -> tuple[bool, bool]:
    window = in_window(cfg, now)
    start, end = _window_minutes(cfg)
    rose = last_in_window is False and window
    # start==end is always in-window, so the next window is the next local date.
    if start == end and last_local_date is not None:
        if _local_dt(cfg, now).date() != last_local_date:
            rose = True
    if (
        cfg.get("mode") == "schedule"
        and rose
        and cfg.get("paused_until_next_window")
    ):
        cfg["paused_until_next_window"] = False
        sync_legacy_from_mode(cfg)
        return window, True
    return window, False


class Watchdog:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_in_window: bool | None = None
        self._last_local_date: date | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vtests-wd", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def tick(self, now: datetime | None = None) -> None:
        cfg = load_config()
        last_window = self._last_in_window
        last_date = self._last_local_date
        window, mutated = clear_pause_on_rising_edge(
            cfg, last_window, now, last_local_date=last_date
        )
        if mutated:
            def _clear_pause(cur: dict[str, Any]) -> None:
                clear_pause_on_rising_edge(
                    cur, last_window, now, last_local_date=last_date
                )

            cfg = update_config(_clear_pause)
        self._last_in_window = window
        self._last_local_date = _local_dt(cfg, now).date()
        want = should_run(cfg, now)
        alive = self.engine.alive()
        if want and not alive:
            self.engine.start(cfg)
        elif not want and alive:
            self.engine.stop()

    def _loop(self) -> None:
        while not self._stop.wait(2):
            self.tick()
