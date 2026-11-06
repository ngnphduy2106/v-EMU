from __future__ import annotations

import time
import unittest

from vemu.ui.host_metrics import HostMetrics, format_metrics


class HostMetricsTests(unittest.TestCase):
    def test_format(self) -> None:
        text = format_metrics(
            {
                "cpu_percent": 12.4,
                "mem_total_mib": 16384,
                "mem_used_mib": 8192,
                "swap_used_mib": 512,
                "net_rx_kib_s": 120,
                "net_tx_kib_s": 30,
            }
        )
        self.assertIn("CPU 12%", text)
        self.assertIn("RAM 8.0/16.0G", text)
        self.assertIn("SWAP 0.5G", text)
        self.assertIn("NET ↓120K ↑30K", text)

    def test_format_network_switches_to_megabytes(self) -> None:
        text = format_metrics(
            {
                "cpu_percent": 1.0,
                "mem_total_mib": 16384,
                "mem_used_mib": 8192,
                "swap_used_mib": 0,
                "net_rx_kib_s": 1536,
                "net_tx_kib_s": 1024,
            }
        )
        self.assertIn("NET ↓1.5M ↑1.0M", text)
        below = format_metrics(
            {
                "cpu_percent": 1.0,
                "mem_total_mib": 16384,
                "mem_used_mib": 8192,
                "swap_used_mib": 0,
                "net_rx_kib_s": 1023,
                "net_tx_kib_s": 0,
            }
        )
        self.assertIn("NET ↓1023K ↑0K", below)

    def test_sample_shape(self) -> None:
        metrics = HostMetrics()
        first = metrics.sample()
        for key in (
            "cpu_percent",
            "mem_total_mib",
            "mem_used_mib",
            "swap_used_mib",
            "net_rx_kib_s",
            "net_tx_kib_s",
        ):
            self.assertIn(key, first)
        time.sleep(0.01)
        second = metrics.sample()
        self.assertIsNotNone(second["mem_total_mib"])
        self.assertIsNotNone(second["mem_used_mib"])


if __name__ == "__main__":
    unittest.main()
