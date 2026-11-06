from __future__ import annotations

from pathlib import Path
import unittest

from vemu.ui.theme import LIGHT, stylesheet
from vemu.ui.tokens import COMPONENT, CONTROL, RADIUS, SPACE, TYPE


class DesignTokenTests(unittest.TestCase):
    def test_type_scale_is_ordered(self) -> None:
        sizes = [
            TYPE.micro,
            TYPE.small,
            TYPE.body_sm,
            TYPE.body,
            TYPE.body_lg,
            TYPE.subtitle,
            TYPE.section,
            TYPE.title,
        ]
        self.assertEqual(sizes, sorted(sizes))

    def test_spacing_and_radius_are_ordered(self) -> None:
        self.assertEqual(
            [SPACE.xxs, SPACE.xs, SPACE.sm, SPACE.md, SPACE.lg, SPACE.xl],
            [2, 4, 8, 12, 16, 24],
        )
        self.assertLess(RADIUS.xs, RADIUS.xl)
        self.assertLess(CONTROL.height_sm, CONTROL.height_lg)
        self.assertGreater(COMPONENT.page_margin, 0)

    def test_stylesheet_uses_tokens(self) -> None:
        style = stylesheet(LIGHT)
        self.assertIn(f"font-size: {TYPE.title}px;", style)
        self.assertIn(f"border-radius: {RADIUS.xl}px;", style)
        self.assertIn("QLabel#DialogTitle", style)
        self.assertIn("QGroupBox", style)

    def test_no_inline_font_size_styles(self) -> None:
        ui = Path(__file__).parents[1] / "src" / "vemu" / "ui"
        for path in ui.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                'setStyleSheet("font-size',
                text,
                f"inline font-size style in {path.name}",
            )
