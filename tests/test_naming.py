from __future__ import annotations

import unittest

from vemu.errors import ValidationError
from vemu.naming import render, render_names, validate_pattern


class NamingTests(unittest.TestCase):
    def test_render(self) -> None:
        self.assertEqual(render("sds-{{n}}", 1), "sds-1")
        self.assertEqual(render("sds-{{n}}", 12), "sds-12")

    def test_render_without_token_single(self) -> None:
        self.assertEqual(render_names("node1", 1), ["node1"])

    def test_render_without_token_multiple_fails(self) -> None:
        with self.assertRaises(ValidationError):
            render_names("node", 3)

    def test_multiple_tokens_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            validate_pattern("{{n}}-{{n}}")

    def test_path_separator_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            validate_pattern("foo/bar-{{n}}")

    def test_duplicate_names_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            render_names("fixed", 2)

    def test_existing_collision_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            render_names("vm-{{n}}", 2, existing={"vm-2"})

    def test_count_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            render_names("vm-{{n}}", 0)
        with self.assertRaises(ValidationError):
            render_names("vm-{{n}}", 101)


if __name__ == "__main__":
    unittest.main()
