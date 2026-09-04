from __future__ import annotations

from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from app.config import migrate_config, public_config
from app.scheduler import apply_start, apply_stop, in_window, should_run


def _cfg(**kwargs):
    base = {
        "mode": "off",
        "paused_until_next_window": False,
        "schedule_start": "09:00",
        "schedule_end": "22:00",
        "timezone": "UTC",
        "enabled": False,
        "schedule_enabled": False,
        "paused": False,
    }
    base.update(kwargs)
    return base


def _at(hour: int, minute: int, tz: str = "UTC") -> datetime:
    return datetime(2026, 1, 15, hour, minute, tzinfo=ZoneInfo(tz))


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (21, 59, False),
        (22, 0, True),
        (23, 30, True),
        (0, 0, True),
        (5, 59, True),
        (6, 0, False),
        (12, 0, False),
    ],
)
def test_overnight_wrap(hour, minute, expected):
    cfg = _cfg(schedule_start="22:00", schedule_end="06:00")
    assert in_window(cfg, _at(hour, minute)) is expected


@pytest.mark.parametrize("hour", [0, 8, 9, 12, 23])
def test_start_equals_end_is_all_day(hour):
    cfg = _cfg(schedule_start="09:00", schedule_end="09:00")
    assert in_window(cfg, _at(hour, 0)) is True


def test_same_day_half_open():
    cfg = _cfg(schedule_start="09:00", schedule_end="22:00")
    assert in_window(cfg, _at(8, 59)) is False
    assert in_window(cfg, _at(9, 0)) is True
    assert in_window(cfg, _at(21, 59)) is True
    assert in_window(cfg, _at(22, 0)) is False


def test_in_window_uses_timezone():
    cfg = _cfg(schedule_start="09:00", schedule_end="10:00", timezone="Asia/Shanghai")
    # 09:00 CST = 01:00 UTC
    assert in_window(cfg, datetime(2026, 6, 1, 1, 0, tzinfo=ZoneInfo("UTC"))) is True
    assert in_window(cfg, datetime(2026, 6, 1, 2, 0, tzinfo=ZoneInfo("UTC"))) is False


@pytest.mark.parametrize(
    ("mode", "paused", "hour", "expected"),
    [
        ("off", False, 12, False),
        ("off", True, 12, False),
        ("manual", False, 3, True),
        ("manual", True, 12, False),
        ("schedule", False, 12, True),
        ("schedule", False, 3, False),
        ("schedule", True, 12, False),
        ("schedule", True, 3, False),
    ],
)
def test_should_run_table(mode, paused, hour, expected):
    cfg = _cfg(
        mode=mode,
        paused_until_next_window=paused,
        schedule_start="09:00",
        schedule_end="22:00",
    )
    assert should_run(cfg, _at(hour, 0)) is expected


def test_migrate_schedule_enabled():
    cfg = migrate_config(
        {
            "enabled": False,
            "schedule_enabled": True,
            "paused": False,
        }
    )
    assert cfg["mode"] == "schedule"
    assert cfg["paused_until_next_window"] is False
    assert cfg["schedule_enabled"] is True
    assert cfg["paused"] is False


def test_migrate_enabled_manual():
    cfg = migrate_config(
        {
            "enabled": True,
            "schedule_enabled": False,
            "paused": False,
        }
    )
    assert cfg["mode"] == "manual"
    assert cfg["paused_until_next_window"] is False
    assert cfg["enabled"] is True
    assert cfg["schedule_enabled"] is False


def test_migrate_paused_with_schedule():
    cfg = migrate_config(
        {
            "enabled": True,
            "schedule_enabled": True,
            "paused": True,
        }
    )
    assert cfg["mode"] == "schedule"
    assert cfg["paused_until_next_window"] is True
    assert cfg["paused"] is True


def test_paused_enabled_false_does_not_become_schedule_on_start():
    cfg = migrate_config(
        {
            "enabled": False,
            "paused": True,
            "schedule_enabled": False,
        }
    )
    assert cfg["mode"] == "off"
    assert cfg["paused_until_next_window"] is False
    apply_start(cfg)
    assert cfg["mode"] == "manual"
    assert cfg["paused_until_next_window"] is False
    assert cfg["schedule_enabled"] is False
    assert cfg["enabled"] is True
    assert cfg["paused"] is False


def test_start_schedule_clears_pause_keeps_schedule():
    cfg = _cfg(mode="schedule", paused_until_next_window=True)
    apply_start(cfg)
    assert cfg["mode"] == "schedule"
    assert cfg["paused_until_next_window"] is False
    assert cfg["paused"] is False
    assert cfg["schedule_enabled"] is True


def test_start_off_becomes_manual_not_schedule():
    cfg = _cfg(mode="off", paused_until_next_window=True)
    apply_start(cfg)
    assert cfg["mode"] == "manual"
    assert cfg["paused_until_next_window"] is False
    assert cfg["schedule_enabled"] is False


def test_stop_schedule_pauses():
    cfg = _cfg(mode="schedule", paused_until_next_window=False)
    apply_stop(cfg)
    assert cfg["mode"] == "schedule"
    assert cfg["paused_until_next_window"] is True
    assert cfg["paused"] is True
    assert cfg["schedule_enabled"] is True
    assert cfg["enabled"] is True


def test_stop_manual_goes_off():
    cfg = _cfg(mode="manual")
    apply_stop(cfg)
    assert cfg["mode"] == "off"
    assert cfg["paused_until_next_window"] is False
    assert cfg["enabled"] is False
    assert cfg["paused"] is False
    assert cfg["schedule_enabled"] is False


def test_public_config_keeps_legacy_keys():
    cfg = migrate_config({"enabled": True, "schedule_enabled": False, "paused": False})
    pub = public_config(cfg)
    assert "enabled" in pub
    assert "schedule_enabled" in pub
    assert "paused" in pub
    assert pub["mode"] == "manual"
    assert pub["paused_until_next_window"] is False
    assert pub["enabled"] is True
    assert pub["schedule_enabled"] is False
