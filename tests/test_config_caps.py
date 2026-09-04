from __future__ import annotations

import json

from app.config import (
    capped_cpu_mem,
    default_config,
    load_config,
    max_cpu_percent,
    max_memory_mb,
    save_config,
    sidecar_lock_path,
    update_config,
)


def test_954_550_memory_cap_128():
    assert max_memory_mb(954, 550) == 128


def test_954_300_memory_cap_0():
    assert max_memory_mb(954, 300) == 0


def test_1gb_cpu_cap_30():
    assert max_cpu_percent(1024) == 30
    assert max_cpu_percent(954) == 30
    assert max_cpu_percent(1536) == 30


def test_cpu_cap_tiers():
    assert max_cpu_percent(1537) == 80
    assert max_cpu_percent(4096) == 80
    assert max_cpu_percent(4097) == 100


def test_rejects_memtotal_minus_512():
    # cap must not be the spike total-512 value
    assert max_memory_mb(954, 550) != 442
    assert max_memory_mb(954, 550) != 954 - 512
    assert max_memory_mb(954, 550) == 128
    assert max_memory_mb(954, 550) < 442


def _patch_small_host(monkeypatch) -> None:
    info = {"total_mb": 954, "avail_mb": 550, "used_mb": 404}

    def fake_meminfo():
        return dict(info)

    monkeypatch.setattr("app.config.meminfo", fake_meminfo)
    monkeypatch.setattr("app.metrics.meminfo", fake_meminfo)


def test_lockfile_exists_beside_json(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    _patch_small_host(monkeypatch)
    save_config(
        {
            "listen": "0.0.0.0",
            "port": 8088,
            "base_path": "/abc",
            "password": "pw",
            "secret": "aa",
            "cpu_percent": 10,
            "memory_mb": 64,
            "mode": "off",
            "paused_until_next_window": False,
            "schedule_start": "09:00",
            "schedule_end": "22:00",
            "timezone": "UTC",
        }
    )
    assert cfg_path.exists()
    lock = sidecar_lock_path(cfg_path)
    assert lock == tmp_path / "config.json.lock"
    assert lock.exists()


def test_round_trip_save_load_keeps_unknown_fields(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    _patch_small_host(monkeypatch)
    save_config(
        {
            "listen": "127.0.0.1",
            "port": 45123,
            "base_path": "/xK92abQ1",
            "password": "s3cret",
            "secret": "deadbeef",
            "cpu_percent": 10,
            "memory_mb": 64,
            "mode": "off",
            "paused_until_next_window": False,
            "schedule_start": "09:00",
            "schedule_end": "22:00",
            "timezone": "Asia/Shanghai",
            "custom_future_field": 123,
        }
    )
    loaded = load_config()
    assert loaded["cpu_percent"] == 10
    assert loaded["memory_mb"] == 64
    assert loaded["mode"] == "off"
    assert loaded["port"] == 45123
    assert loaded["base_path"] == "/xK92abQ1"
    assert loaded["password"] == "s3cret"
    assert loaded["custom_future_field"] == 123
    assert loaded["enabled"] is False
    assert loaded["schedule_enabled"] is False
    assert loaded["paused"] is False
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert raw["custom_future_field"] == 123
    assert raw["mode"] == "off"


def test_save_clamps_cpu_and_memory(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    _patch_small_host(monkeypatch)
    save_config(
        {
            "port": 8088,
            "base_path": "/x",
            "password": "pw",
            "secret": "aa",
            "cpu_percent": 100,
            "memory_mb": 999,
            "mode": "off",
            "schedule_start": "09:00",
            "schedule_end": "22:00",
            "timezone": "UTC",
        }
    )
    loaded = load_config()
    assert loaded["cpu_percent"] == 30
    assert loaded["memory_mb"] == 128


def test_load_migrates_spike_keys(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    _patch_small_host(monkeypatch)
    cfg_path.write_text(
        json.dumps(
            {
                "listen": "0.0.0.0",
                "port": 8088,
                "base_path": "/old",
                "password": "pw",
                "secret": "aa",
                "cpu_percent": 10,
                "memory_mb": 64,
                "schedule_enabled": False,
                "schedule_start": "09:00",
                "schedule_end": "22:00",
                "timezone": "UTC",
                "enabled": False,
                "paused": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = load_config()
    assert loaded["mode"] == "off"
    assert loaded["paused_until_next_window"] is False


def test_small_host_defaults(monkeypatch):
    _patch_small_host(monkeypatch)
    cfg = default_config()
    assert cfg["cpu_percent"] == 10
    assert cfg["memory_mb"] == 64
    assert cfg["mode"] == "off"
    assert cfg["paused_until_next_window"] is False


def test_avail_dip_does_not_persist_memory_mb(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    host = {"total_mb": 954, "avail_mb": 550, "used_mb": 404}

    def fake_meminfo():
        return dict(host)

    monkeypatch.setattr("app.config.meminfo", fake_meminfo)
    monkeypatch.setattr("app.metrics.meminfo", fake_meminfo)
    save_config(
        {
            "port": 8088,
            "base_path": "/x",
            "password": "pw",
            "secret": "aa",
            "cpu_percent": 10,
            "memory_mb": 64,
            "mode": "schedule",
            "paused_until_next_window": True,
            "schedule_start": "09:00",
            "schedule_end": "22:00",
            "timezone": "UTC",
        }
    )
    host["avail_mb"] = 300

    def _clear_pause(cfg):
        cfg["paused_until_next_window"] = False

    update_config(_clear_pause)
    cpu, mem = capped_cpu_mem(load_config(), 954, 300)
    assert cpu == 10
    assert mem == 0
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert raw["memory_mb"] == 64
    host["avail_mb"] = 550
    loaded = load_config()
    assert loaded["memory_mb"] == 64
    assert loaded["cpu_percent"] == 10
    assert loaded["paused_until_next_window"] is False


def test_user_save_during_dip_keeps_requested_memory(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("VTESTS_CONFIG", str(cfg_path))
    host = {"total_mb": 954, "avail_mb": 300, "used_mb": 654}

    def fake_meminfo():
        return dict(host)

    monkeypatch.setattr("app.config.meminfo", fake_meminfo)
    monkeypatch.setattr("app.metrics.meminfo", fake_meminfo)
    save_config(
        {
            "port": 8088,
            "base_path": "/x",
            "password": "pw",
            "secret": "aa",
            "cpu_percent": 10,
            "memory_mb": 64,
            "mode": "off",
            "schedule_start": "09:00",
            "schedule_end": "22:00",
            "timezone": "UTC",
        }
    )
    loaded = load_config()
    assert loaded["memory_mb"] == 64
    assert capped_cpu_mem(loaded, 954, 300)[1] == 0
