"""Lightweight host load sampling for the status bar."""

from __future__ import annotations

import time
from pathlib import Path


def _cpu_times() -> tuple[int, int] | None:
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    fields = line.split()
    if not fields or fields[0] != "cpu":
        return None
    values = [int(value) for value in fields[1:]]
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _memory() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        key, _, rest = line.partition(":")
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            try:
                values[key] = int(rest.split()[0])
            except (IndexError, ValueError):
                continue
    return values


def _network() -> tuple[int, int]:
    rx = 0
    tx = 0
    try:
        lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()
    except OSError:
        return rx, tx
    for line in lines[2:]:
        iface, _, data = line.partition(":")
        if iface.strip() == "lo":
            continue
        fields = data.split()
        if len(fields) < 9:
            continue
        try:
            rx += int(fields[0])
            tx += int(fields[8])
        except ValueError:
            continue
    return rx, tx


def _format_rate(kib_per_s: float) -> str:
    """Format KiB/s as K below 1 MiB/s and otherwise as M."""
    if kib_per_s >= 1024:
        return f"{kib_per_s / 1024:.1f}M"
    return f"{kib_per_s:.0f}K"


def format_metrics(metrics: dict[str, float | int | None]) -> str:
    cpu = metrics.get("cpu_percent")
    total = metrics.get("mem_total_mib")
    used = metrics.get("mem_used_mib")
    swap_used = metrics.get("swap_used_mib")
    rx = metrics.get("net_rx_kib_s")
    tx = metrics.get("net_tx_kib_s")
    parts = [f"CPU {cpu:.0f}%" if cpu is not None else "CPU —"]
    if total and used is not None:
        parts.append(f"RAM {used / 1024:.1f}/{total / 1024:.1f}G")
    else:
        parts.append("RAM —")
    parts.append(
        f"SWAP {swap_used / 1024:.1f}G" if swap_used is not None else "SWAP —"
    )
    if rx is not None and tx is not None:
        parts.append(
            f"NET ↓{_format_rate(float(rx))} ↑{_format_rate(float(tx))}"
        )
    else:
        parts.append("NET —")
    return " · ".join(parts)


class HostMetrics:
    def __init__(self) -> None:
        self._last_cpu: tuple[int, int, float] | None = None
        self._last_net: tuple[int, int, float] | None = None

    def sample(self) -> dict[str, float | int | None]:
        now = time.monotonic()
        cpu = _cpu_times()
        cpu_percent: float | None = None
        if cpu is not None:
            if self._last_cpu is not None:
                total_delta = cpu[0] - self._last_cpu[0]
                idle_delta = cpu[1] - self._last_cpu[1]
                if total_delta > 0:
                    cpu_percent = max(
                        0.0,
                        min(100.0, 100.0 * (1.0 - idle_delta / total_delta)),
                    )
            self._last_cpu = (cpu[0], cpu[1], now)

        mem = _memory()
        total = mem.get("MemTotal", 0)
        available = mem.get("MemAvailable", mem.get("MemFree", 0))
        used = max(0, total - available)
        swap_total = mem.get("SwapTotal", 0)
        swap_free = mem.get("SwapFree", 0)

        rx, tx = _network()
        rx_rate = tx_rate = None
        if self._last_net is not None:
            elapsed = max(0.001, now - self._last_net[2])
            rx_rate = max(0.0, (rx - self._last_net[0]) / 1024 / elapsed)
            tx_rate = max(0.0, (tx - self._last_net[1]) / 1024 / elapsed)
        self._last_net = (rx, tx, now)

        return {
            "cpu_percent": cpu_percent,
            "mem_total_mib": total // 1024 if total else None,
            "mem_used_mib": used // 1024 if total else None,
            "swap_total_mib": swap_total // 1024 if swap_total else 0,
            "swap_used_mib": max(0, swap_total - swap_free) // 1024
            if swap_total
            else 0,
            "net_rx_kib_s": rx_rate,
            "net_tx_kib_s": tx_rate,
        }
