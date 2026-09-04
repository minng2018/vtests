from __future__ import annotations

import signal
from unittest.mock import MagicMock

from app.engine import (
    BANNED_FLAGS,
    CGROUP_OOM_ERROR,
    LoadEngine,
    build_stress_cmd,
    oom_avoid_bytes,
)

BINARY = "/usr/bin/stress-ng"
LOG = "/var/log/vtests/stress-ng.log"


def _cmd(
    cpu: int,
    mem: int,
    total_mb: int = 954,
    **kwargs,
) -> list[str] | None:
    kwargs.setdefault("log_file", LOG)
    kwargs.setdefault("nice_bin", "nice")
    kwargs.setdefault("ionice_bin", "ionice")
    return build_stress_cmd(BINARY, cpu, mem, total_mb, **kwargs)


def test_cpu_only_omits_vm():
    cmd = _cmd(cpu=10, mem=0)
    assert cmd is not None
    assert "--cpu" in cmd
    assert "--cpu-load" in cmd
    assert cmd[cmd.index("--cpu-load") + 1] == "10"
    assert "--cpu-method" in cmd
    assert cmd[cmd.index("--cpu-method") + 1] == "nop"
    assert "--vm" not in cmd
    assert "--vm-bytes" not in cmd
    assert "--vm-keep" not in cmd


def test_mem_only_omits_cpu():
    cmd = _cmd(cpu=0, mem=64)
    assert cmd is not None
    assert "--vm" in cmd
    assert "--vm-bytes" in cmd
    assert cmd[cmd.index("--vm-bytes") + 1] == "64M"
    assert "--vm-keep" in cmd
    assert "--cpu" not in cmd
    assert "--cpu-load" not in cmd
    assert "--cpu-method" not in cmd


def test_both_cpu_and_mem():
    cmd = _cmd(cpu=10, mem=64)
    assert cmd is not None
    assert cmd[:6] == ["nice", "-n", "19", "ionice", "-c", "3"]
    assert BINARY in cmd
    assert "--timeout" in cmd
    assert cmd[cmd.index("--timeout") + 1] == "0"
    assert "--cpu-load" in cmd
    assert cmd[cmd.index("--cpu-load") + 1] == "10"
    assert "--vm-bytes" in cmd
    assert cmd[cmd.index("--vm-bytes") + 1] == "64M"
    assert "--log-file" in cmd
    assert cmd[cmd.index("--log-file") + 1] == LOG
    assert "--log-brief" in cmd
    assert "--quiet" in cmd


def test_both_zero_does_not_build():
    assert _cmd(cpu=0, mem=0) is None


def test_cpu_method_fallback_nop():
    cmd = _cmd(cpu=10, mem=0, cpu_method="all")
    assert cmd is not None
    assert cmd[cmd.index("--cpu-method") + 1] == "nop"
    cmd = _cmd(cpu=10, mem=0, cpu_method="matrixprod")
    assert cmd is not None
    assert cmd[cmd.index("--cpu-method") + 1] == "nop"
    cmd = _cmd(cpu=10, mem=0, cpu_method="nop")
    assert cmd is not None
    assert cmd[cmd.index("--cpu-method") + 1] == "nop"


def test_oom_avoid_bytes_128m_on_1gb():
    assert oom_avoid_bytes(954) == "128M"
    assert oom_avoid_bytes(1024) == "128M"
    assert oom_avoid_bytes(1536) == "128M"
    cmd = _cmd(cpu=10, mem=64, total_mb=954)
    assert cmd is not None
    assert "--oom-avoid" in cmd
    assert "--oom-avoid-bytes" in cmd
    assert cmd[cmd.index("--oom-avoid-bytes") + 1] == "128M"
    assert "256M" not in cmd


def test_oom_avoid_bytes_256m_on_larger_hosts():
    assert oom_avoid_bytes(1537) == "256M"
    assert oom_avoid_bytes(4096) == "256M"
    cmd = _cmd(cpu=20, mem=128, total_mb=2048)
    assert cmd is not None
    assert cmd[cmd.index("--oom-avoid-bytes") + 1] == "256M"


def test_no_banned_flags():
    cmd = _cmd(cpu=10, mem=64)
    assert cmd is not None
    for flag in BANNED_FLAGS:
        assert flag not in cmd
    assert "--pathological" not in cmd
    assert "--thrash" not in cmd
    assert "--ignite-cpu" not in cmd
    assert "--hdd" not in cmd
    assert "--sock" not in cmd


def test_keep_name_and_no_oom_adjust():
    cmd = _cmd(cpu=10, mem=64)
    assert cmd is not None
    assert "--keep-name" in cmd
    assert "--no-oom-adjust" in cmd
    assert "--oom-avoid" in cmd


def test_skips_ionice_when_missing():
    cmd = _cmd(cpu=10, mem=0, ionice_bin=None)
    assert cmd is not None
    assert cmd[:3] == ["nice", "-n", "19"]
    assert "ionice" not in cmd


def test_nice_out_of_range_defaults_19():
    cmd = _cmd(cpu=10, mem=0, nice=99, ionice_bin=None)
    assert cmd is not None
    assert cmd[:3] == ["nice", "-n", "19"]


def test_engine_start_omits_vm_when_avail_low(monkeypatch):
    captured: dict = {}

    def fake_which(name: str):
        if name == "stress-ng":
            return BINARY
        return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 4242
        return proc

    monkeypatch.setattr(
        "app.engine.meminfo",
        lambda: {"total_mb": 954, "avail_mb": 300, "used_mb": 654},
    )
    monkeypatch.setattr("app.engine.shutil.which", fake_which)
    monkeypatch.setattr("app.engine.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.engine.ensure_log_file", lambda: LOG)
    monkeypatch.setattr("app.engine.workdir", lambda: None)

    engine = LoadEngine()
    engine.start({"cpu_percent": 10, "memory_mb": 64})
    cmd = captured["cmd"]
    assert "--cpu-load" in cmd
    assert cmd[cmd.index("--cpu-load") + 1] == "10"
    assert "--vm" not in cmd
    assert captured["kwargs"]["start_new_session"] is True


def test_engine_start_both_zero_does_not_popen(monkeypatch):
    called = {"n": 0}

    def fake_popen(*_a, **_k):
        called["n"] += 1
        raise AssertionError("Popen must not run when cpu and mem are 0")

    monkeypatch.setattr(
        "app.engine.meminfo",
        lambda: {"total_mb": 954, "avail_mb": 550, "used_mb": 404},
    )
    monkeypatch.setattr("app.engine.shutil.which", lambda name: BINARY if name == "stress-ng" else None)
    monkeypatch.setattr("app.engine.subprocess.Popen", fake_popen)

    engine = LoadEngine()
    engine.start({"cpu_percent": 0, "memory_mb": 0})
    assert called["n"] == 0
    assert engine.alive() is False


def test_cgroup_sigkill_sets_engine_fail():
    engine = LoadEngine()
    proc = MagicMock()
    proc.poll.return_value = -signal.SIGKILL
    proc.pid = 9
    engine._proc = proc
    engine._stopping = False
    assert engine.alive() is False
    assert engine.error() == CGROUP_OOM_ERROR
