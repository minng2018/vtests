#!/usr/bin/env python3
"""Assemble and run a constrained stress-ng child."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

from app.config import capped_cpu_mem
from app.metrics import meminfo

log = logging.getLogger("vtests.engine")

BANNED_FLAGS = ("--pathological", "--thrash", "--ignite-cpu", "--hdd", "--sock")
ALLOWED_CPU_METHODS = frozenset({"nop", "loop"})
CGROUP_OOM_ERROR = "cgroup OOM / MemoryMax"
DEFAULT_LOG_DIR = Path("/var/log/vtests")
DEFAULT_STATE_DIR = Path("/var/lib/vtests")
STRESS_LOG_NAME = "stress-ng.log"
MISSING_BINARY = "未找到 stress-ng，请安装后重启服务"


def oom_avoid_bytes(total_mb: int) -> str:
    # 256M on 1 GB hosts would refuse the 64 MB default vm.
    if total_mb <= 1536:
        return "128M"
    return "256M"


def normalize_cpu_method(value: Any) -> str:
    method = str(value or "loop").strip()
    # noble stress-ng 0.17.06 has no nop; loop is the equivalent idle-burn method.
    if method == "nop":
        method = "loop"
    if method in ALLOWED_CPU_METHODS:
        return method
    return "loop"


def normalize_nice(value: Any) -> int:
    try:
        nice = int(value)
    except (TypeError, ValueError):
        return 19
    if nice < 0 or nice > 19:
        return 19
    return nice


def log_dir() -> Path:
    env = os.environ.get("VTESTS_LOG_DIR")
    if env:
        return Path(env)
    return DEFAULT_LOG_DIR


def log_file_path() -> Path:
    return log_dir() / STRESS_LOG_NAME


def workdir() -> str | None:
    if DEFAULT_STATE_DIR.is_dir():
        return str(DEFAULT_STATE_DIR)
    return None


def ensure_log_file() -> Path | None:
    directory = log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        if not directory.is_dir():
            return None
    path = directory / STRESS_LOG_NAME
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        os.close(fd)
    except OSError:
        return None
    return path


def build_stress_cmd(
    binary: str,
    cpu: int,
    mem: int,
    total_mb: int,
    *,
    cpu_method: Any = "loop",
    nice: Any = 19,
    log_file: str | os.PathLike[str] | None = None,
    nice_bin: str | None = None,
    ionice_bin: str | None = None,
) -> list[str] | None:
    if cpu <= 0 and mem <= 0:
        return None
    cmd: list[str] = []
    if nice_bin:
        cmd.extend([nice_bin, "-n", str(normalize_nice(nice))])
    if ionice_bin:
        cmd.extend([ionice_bin, "-c", "3"])
    cmd.append(binary)
    cmd.extend(
        [
            "--timeout",
            "0",
            "--no-oom-adjust",
            "--oom-avoid",
            "--oom-avoid-bytes",
            oom_avoid_bytes(total_mb),
            "--keep-name",
        ]
    )
    if cpu > 0:
        cmd.extend(
            [
                "--cpu",
                "0",
                "--cpu-load",
                str(cpu),
                "--cpu-method",
                normalize_cpu_method(cpu_method),
            ]
        )
    if mem > 0:
        cmd.extend(["--vm", "1", "--vm-bytes", f"{mem}M", "--vm-keep"])
    if log_file:
        cmd.extend(["--log-file", str(log_file), "--log-brief", "--quiet"])
    banned = set(BANNED_FLAGS).intersection(cmd)
    if banned:
        raise ValueError(f"banned stress-ng flags: {sorted(banned)}")
    return cmd


def _exit_was_sigkill(code: int | None) -> bool:
    if code is None:
        return False
    return code == -signal.SIGKILL or code == 128 + signal.SIGKILL


class LoadEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._error = ""
        self._stopping = False
        self._cmd: list[str] = []

    def alive(self) -> bool:
        with self._lock:
            self._reap_locked()
            return self._proc is not None and self._proc.poll() is None

    def error(self) -> str:
        with self._lock:
            self._reap_locked()
            return self._error

    def last_log_lines(self, n: int = 20) -> list[str]:
        path = log_file_path()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        if n <= 0:
            return []
        return text.splitlines()[-n:]

    def start(self, cfg: dict[str, Any]) -> None:
        binary = shutil.which("stress-ng")
        if not binary:
            self._error = MISSING_BINARY
            log.error("event=engine_fail error=%s", self._error)
            return
        info = meminfo()
        cpu, mem = capped_cpu_mem(dict(cfg), info["total_mb"], info["avail_mb"])
        if cpu <= 0 and mem <= 0:
            self.stop()
            return
        log_path = ensure_log_file()
        cmd = build_stress_cmd(
            binary,
            cpu,
            mem,
            info["total_mb"],
            cpu_method=cfg.get("cpu_method") or "loop",
            nice=cfg.get("nice", 19),
            log_file=log_path,
            nice_bin=shutil.which("nice"),
            ionice_bin=shutil.which("ionice"),
        )
        if not cmd:
            self.stop()
            return
        cwd = workdir()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._kill_locked()
            try:
                self._stopping = False
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    cwd=cwd,
                )
                self._cmd = cmd
                self._error = ""
                log.info(
                    "event=engine_start cpu_percent=%s memory_mb=%s",
                    cpu,
                    mem,
                )
            except OSError as exc:
                self._error = str(exc)
                self._proc = None
                log.error("event=engine_fail error=%s", self._error)

    def stop(self) -> None:
        with self._lock:
            had = self._proc is not None
            self._kill_locked()
            if had:
                log.info("event=engine_stop")

    def _reap_locked(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            return
        self._proc = None
        if self._stopping:
            return
        if _exit_was_sigkill(code):
            self._error = CGROUP_OOM_ERROR
            log.error("event=engine_fail error=%s", self._error)

    def _kill_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        self._stopping = True
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
