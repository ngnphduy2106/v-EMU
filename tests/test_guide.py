from __future__ import annotations

import unittest

from vemu.ui.markdown import render_markdown


class MarkdownTests(unittest.TestCase):
    def test_headings_paragraph_and_inline(self) -> None:
        html = render_markdown(
            "# Title\n\nUse `vemu` with **bold** and [docs](https://example.test)."
        )
        self.assertIn("<h1>Title</h1>", html)
        self.assertIn("<code>vemu</code>", html)
        self.assertIn("<b>bold</b>", html)
        self.assertIn('<a href="https://example.test">docs</a>', html)

    def test_fenced_code_is_escaped(self) -> None:
        html = render_markdown(
            "```bash\nvemu up <lab.yaml>\n```\n"
        )
        self.assertIn('<pre><code class="language-bash">', html)
        self.assertIn("vemu up &lt;lab.yaml&gt;", html)

    def test_table_renders_header_and_rows(self) -> None:
        html = render_markdown(
            "| Name | Value |\n"
            "| --- | --- |\n"
            "| one | `1` |\n"
        )
        self.assertIn("<table", html)
        self.assertIn("<th>Name</th>", html)
        self.assertIn("<td>one</td>", html)
        self.assertIn("<code>1</code>", html)

    def test_list_continuation_stays_in_item(self) -> None:
        html = render_markdown(
            "- first line\n"
            "  continued line\n"
            "- second\n"
        )
        self.assertIn("<li>first line continued line</li>", html)
        self.assertIn("<li>second</li>", html)

    def test_blockquote(self) -> None:
        html = render_markdown("> note\n>\n> more\n")
        self.assertIn("<blockquote>", html)
        self.assertIn("note", html)
        self.assertIn("more", html)


if __name__ == "__main__":
    unittest.main()
