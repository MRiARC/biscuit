"""Optional Linux host-memory sampling; NOT full-stack 8 GiB certification."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from typing import Self


def linux_host_used_mib() -> float | None:
    """Return used host memory from /proc; not cgroup RSS nor per-model memory."""
    try:
        rows = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    numbers: dict[str, int] = {}
    for row in rows:
        parts = row.split()
        if len(parts) >= 2 and parts[0] in {"MemTotal:", "MemAvailable:"}:
            numbers[parts[0]] = int(parts[1])
    if set(numbers) != {"MemTotal:", "MemAvailable:"}:
        return None
    total, available = numbers["MemTotal:"], numbers["MemAvailable:"]
    if total <= 0 or not 0 <= available <= total:
        return None
    return (total - available) / 1024


class HostMemorySampler:
    """A best-effort in-process sampler with explicit partial-evidence status."""

    def __init__(
        self, interval_seconds: float = 0.05,
        source: Callable[[], float | None] = linux_host_used_mib,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Memory sampling interval must be positive")
        self.interval_seconds = interval_seconds
        self.source = source
        self.peak_used_mib: float | None = None
        self.samples = 0
        self._stop = Event()
        self._thread: Thread | None = None

    def _read(self) -> None:
        value = self.source()
        if value is not None and value >= 0:
            self.peak_used_mib = (
                value if self.peak_used_mib is None
                else max(self.peak_used_mib, value)
            )
            self.samples += 1

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._read()

    def __enter__(self) -> Self:
        self._read()
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._read()

    def results(self) -> dict[str, object]:
        return {
            "measurement_status": (
                "PARTIAL_HOST_SAMPLES_ONLY"
                if self.peak_used_mib is not None else "NOT_MEASURED"
            ),
            "peak_host_used_mib": self.peak_used_mib,
            "sample_count": self.samples,
            "sampling_interval_seconds": self.interval_seconds,
            "scope": "Linux MemTotal minus MemAvailable, host-wide, point samples",
            "full_stack_8gib_verified": False,
            "limitations": (
                "No attribution of model, KV cache, database or container RSS; "
                "short peaks can be missed; target-hardware reproduction required"
            ),
        }
