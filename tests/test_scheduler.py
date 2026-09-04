from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from zoneinfo import ZoneInfo

from app.config import load_config, migrate_config, public_config, save_config
from app.scheduler import (
    Watchdog,
    apply_start,
    apply_stop,
    clear_pause_on_rising_edge,
    in_window,
    should_run,
)


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


@pytest.mark.parametrize(
    ("mode", "paused", "action", "hour", "exp_mode", "exp_paused", "exp_run"),
    [
        ("schedule", False, "stop", 12, "schedule", True, False),
        ("schedule", True, "stop", 12, "schedule", True, False),
        ("schedule", False, "stop", 3, "schedule", True, False),
        ("manual", False, "stop", 12, "off", False, False),
        ("off", False, "stop", 12, "off", False, False),
        ("off", True, "stop", 12, "off", False, False),
        ("schedule", True, "start", 12, "schedule", False, True),
        ("schedule", True, "start", 3, "schedule", False, False),
        ("schedule", False, "start", 3, "schedule", False, False),
        ("schedule", False, "start", 12, "schedule", False, True),
        ("off", False, "start", 3, "manual", False, True),
        ("off", True, "start", 12, "manual", False, True),
        ("manual", False, "start", 3, "manual", False, True),
        ("manual", True, "start", 3, "manual", False, True),
    ],
)
def test_start_stop_table(mode, paused, action, hour, exp_mode, exp_paused, exp_run):
    cfg = _cfg(mode=mode, paused_until_next_window=paused)
    if action == "start":
        apply_start(cfg)
    else:
        apply_stop(cfg)
    assert cfg["mode"] == exp_mode
    assert cfg["paused_until_next_window"] is exp_paused
    assert should_run(cfg, _at(hour, 0)) is exp_run
    if exp_mode == "schedule":
        assert cfg["schedule_enabled"] is True
        assert cfg["enabled"] is True
        assert cfg["paused"] is exp_paused
    elif exp_mode == "manual":
        assert cfg["schedule_enabled"] is False
        assert cfg["enabled"] is True
        assert cfg["paused"] is False
    else:
        assert cfg["schedule_enabled"] is False
        assert cfg["enabled"] is False
        assert cfg["paused"] is False


def test_start_schedule_outside_window_waits():
    cfg = _cfg(mode="schedule", paused_until_next_window=True)
    apply_start(cfg)
    assert cfg["mode"] == "schedule"
    assert cfg["paused_until_next_window"] is False
    assert should_run(cfg, _at(3, 0)) is False
    assert should_run(cfg, _at(12, 0)) is True


def test_clear_pause_only_on_false_to_true_edge():
    cfg = _cfg(mode="schedule", paused_until_next_window=True, enabled=True, paused=True)
    window, mutated = clear_pause_on_rising_edge(cfg, True, _at(12, 0))
    assert window is True
    assert mutated is False
    assert cfg["paused_until_next_window"] is True

    window, mutated = clear_pause_on_rising_edge(cfg, None, _at(12, 0))
    assert window is True
    assert mutated is False
    assert cfg["paused_until_next_window"] is True

    window, mutated = clear_pause_on_rising_edge(cfg, False, _at(3, 0))
    assert window is False
    assert mutated is False
    assert cfg["paused_until_next_window"] is True

    window, mutated = clear_pause_on_rising_edge(cfg, False, _at(12, 0))
    assert window is True
    assert mutated is True
    assert cfg["paused_until_next_window"] is False
    assert cfg["paused"] is False
    assert cfg["mode"] == "schedule"
    assert cfg["enabled"] is True
    assert cfg["schedule_enabled"] is True


def test_watchdog_does_not_set_enabled_on_window_edges():
    cfg = _cfg(mode="schedule", paused_until_next_window=False, enabled=True)
    window, mutated = clear_pause_on_rising_edge(cfg, True, _at(3, 0))
    assert window is False
    assert mutated is False
    assert cfg["enabled"] is True
    assert cfg["paused_until_next_window"] is False

    window, mutated = clear_pause_on_rising_edge(cfg, False, _at(12, 0))
    assert window is True
    assert mutated is False
    assert cfg["enabled"] is True
    assert cfg["mode"] == "schedule"


class _FakeEngine:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self._alive = False

    def alive(self) -> bool:
        return self._alive

    def start(self, _cfg) -> None:
        self.starts += 1
        self._alive = True

    def stop(self) -> None:
        self.stops += 1
        self._alive = False


def test_watchdog_tick_rising_edge_clears_pause_only(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    save_config(_cfg(mode="schedule", paused_until_next_window=True))
    engine = _FakeEngine()
    wd = Watchdog(engine)
    wd._last_in_window = False
    wd.tick(now=_at(12, 0))
    loaded = load_config()
    assert loaded["paused_until_next_window"] is False
    assert loaded["paused"] is False
    assert loaded["mode"] == "schedule"
    assert loaded["enabled"] is True
    assert loaded["schedule_enabled"] is True
    assert engine.alive() is True
    assert engine.starts == 1
    assert engine.stops == 0


def test_watchdog_tick_does_not_rewrite_enabled(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    save_config(_cfg(mode="schedule", paused_until_next_window=False))
    engine = _FakeEngine()
    engine.start({})
    wd = Watchdog(engine)
    wd._last_in_window = True
    before = json.loads(cfg_path.read_text(encoding="utf-8"))
    mtime = cfg_path.stat().st_mtime_ns
    wd.tick(now=_at(3, 0))
    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg_path.stat().st_mtime_ns == mtime
    assert after == before
    assert after["enabled"] is True
    assert engine.alive() is False
    assert engine.stops == 1

    wd.tick(now=_at(12, 0))
    after_rise = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg_path.stat().st_mtime_ns == mtime
    assert after_rise == before
    assert after_rise["enabled"] is True
    assert engine.alive() is True
    assert engine.starts == 2


def test_watchdog_first_tick_is_not_rising_edge(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    save_config(_cfg(mode="schedule", paused_until_next_window=True))
    engine = _FakeEngine()
    wd = Watchdog(engine)
    before = json.loads(cfg_path.read_text(encoding="utf-8"))
    wd.tick(now=_at(12, 0))
    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after == before
    assert after["paused_until_next_window"] is True
    assert after["enabled"] is True
    assert engine.alive() is False
    assert engine.starts == 0


def test_all_day_pause_clears_on_local_date_rollover():
    cfg = _cfg(
        mode="schedule",
        paused_until_next_window=True,
        schedule_start="09:00",
        schedule_end="09:00",
        timezone="Asia/Shanghai",
        enabled=True,
        paused=True,
        schedule_enabled=True,
    )
    same_day = datetime(2026, 6, 1, 15, 30, tzinfo=ZoneInfo("UTC"))
    window, mutated = clear_pause_on_rising_edge(
        cfg, True, same_day, last_local_date=date(2026, 6, 1)
    )
    assert window is True
    assert mutated is False
    assert cfg["paused_until_next_window"] is True

    window, mutated = clear_pause_on_rising_edge(
        cfg, None, same_day, last_local_date=None
    )
    assert window is True
    assert mutated is False
    assert cfg["paused_until_next_window"] is True

    next_local_midnight = datetime(2026, 6, 1, 16, 0, tzinfo=ZoneInfo("UTC"))
    window, mutated = clear_pause_on_rising_edge(
        cfg, True, next_local_midnight, last_local_date=date(2026, 6, 1)
    )
    assert window is True
    assert mutated is True
    assert cfg["paused_until_next_window"] is False
    assert cfg["paused"] is False
    assert cfg["mode"] == "schedule"
    assert cfg["enabled"] is True


def test_partial_day_date_rollover_does_not_clear_pause():
    cfg = _cfg(
        mode="schedule",
        paused_until_next_window=True,
        schedule_start="09:00",
        schedule_end="22:00",
        timezone="UTC",
    )
    midnight = datetime(2026, 1, 16, 0, 0, tzinfo=ZoneInfo("UTC"))
    window, mutated = clear_pause_on_rising_edge(
        cfg, False, midnight, last_local_date=date(2026, 1, 15)
    )
    assert window is False
    assert mutated is False
    assert cfg["paused_until_next_window"] is True

    window, mutated = clear_pause_on_rising_edge(
        cfg, False, datetime(2026, 1, 16, 9, 0, tzinfo=ZoneInfo("UTC")),
        last_local_date=date(2026, 1, 16),
    )
    assert window is True
    assert mutated is True
    assert cfg["paused_until_next_window"] is False


def test_watchdog_all_day_pause_clears_at_next_local_midnight(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    save_config(
        _cfg(
            mode="schedule",
            paused_until_next_window=True,
            schedule_start="09:00",
            schedule_end="09:00",
            timezone="UTC",
        )
    )
    engine = _FakeEngine()
    wd = Watchdog(engine)
    wd.tick(now=_at(12, 0))
    assert load_config()["paused_until_next_window"] is True
    assert engine.alive() is False

    wd.tick(now=_at(23, 59))
    assert load_config()["paused_until_next_window"] is True
    assert engine.alive() is False

    wd.tick(now=datetime(2026, 1, 16, 0, 0, tzinfo=ZoneInfo("UTC")))
    loaded = load_config()
    assert loaded["paused_until_next_window"] is False
    assert loaded["paused"] is False
    assert loaded["mode"] == "schedule"
    assert loaded["enabled"] is True
    assert engine.alive() is True
    assert engine.starts == 1
