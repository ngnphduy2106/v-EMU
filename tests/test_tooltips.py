from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vemu.ui.tooltips import (
    TOOLTIP_MAX_LINES,
    TOOLTIP_WRAP,
    compact_tooltip,
    tip,
)


class TooltipTests(unittest.TestCase):
    def test_short_text_is_unchanged(self) -> None:
        self.assertEqual(compact_tooltip("Start this VM"), "Start this VM")

    def test_long_text_wraps_and_limits_lines(self) -> None:
        text = " ".join(["detail"] * 80)
        compact = compact_tooltip(text)
        lines = compact.splitlines()
        self.assertLessEqual(len(lines), TOOLTIP_MAX_LINES)
        self.assertTrue(all(len(line) <= TOOLTIP_WRAP for line in lines))
        self.assertTrue(lines[-1].endswith("…"))
        self.assertEqual(compact_tooltip(compact), compact)

    def test_title_stays_on_the_first_line(self) -> None:
        compact = tip(
            "Guest stats",
            "vCPU average since the last sample. RAM from guest balloon "
            "statistics (total minus usable). Host RSS 9031 MiB.",
        )
        self.assertEqual(compact.splitlines()[0], "Guest stats")
        self.assertIn("usable", compact)
        self.assertIn("9031", compact)



if __name__ == "__main__":
    unittest.main()
