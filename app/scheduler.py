#!/usr/bin/env python3
"""Daily window checks and mode state transitions."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import VALID_MODES, apply_legacy_migration, sync_legacy_from_mode

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
