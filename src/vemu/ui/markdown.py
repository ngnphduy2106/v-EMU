"""Small, dependency-free Markdown renderer for the built-in guide.

Qt's native Markdown support is intentionally small: tables, fenced code and
nested lists render poorly in ``QTextBrowser``.  This renderer covers the
subset used by the guide and emits conservative HTML that QTextDocument can
style consistently.
"""

from __future__ import annotations

import html
import re
from typing import Iterable

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)\s*([\w.+-]*)\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|(?<!_)_(?!_)(.+?)(?<!_)_(?!_)")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


def _inline(text: str) -> str:
    """Render inline Markdown, leaving code spans untouched."""
    pieces: list[str] = []
    for index, chunk in enumerate(re.split(r"(`[^`]*`)", text)):
        if index % 2 and len(chunk) >= 2 and chunk.startswith("`"):
            pieces.append(f"<code>{html.escape(chunk[1:-1])}</code>")
            continue
        value = html.escape(chunk, quote=False)
        value = _LINK_RE.sub(
            lambda match: (
                f'<a href="{html.escape(match.group(2), quote=True)}">'
                f"{match.group(1)}</a>"
            ),
            value,
        )
        value = _BOLD_RE.sub(
            lambda match: f"<b>{match.group(1) or match.group(2)}</b>",
            value,
        )
        value = _ITALIC_RE.sub(
            lambda match: f"<i>{match.group(1) or match.group(2)}</i>",
            value,
        )
        pieces.append(value)
    return "".join(pieces)


def _table_cells(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip() for cell in value.split("|")]


def _is_table(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in lines[index]
        and bool(_TABLE_SEP_RE.match(lines[index + 1]))
    )


def _render_table(lines: list[str], index: int) -> tuple[str, int]:
    header = _table_cells(lines[index])
    rows: list[list[str]] = []
    cursor = index + 2
    while cursor < len(lines):
        line = lines[cursor]
        if not line.strip() or "|" not in line:
            break
        rows.append(_table_cells(line))
        cursor += 1
    head = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
    body = "".join(
        "<tr>"
        + "".join(f"<td>{_inline(cell)}</td>" for cell in row)
        + "</tr>"
        for row in rows
    )
    return (
        f'<table width="100%"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>",
        cursor,
    )


def _render_list(lines: list[str], index: int) -> tuple[str, int]:
    items: list[str] = []
    ordered = False
    cursor = index
    while cursor < len(lines):
        line = lines[cursor]
        match = _LIST_RE.match(line)
        if match is not None:
            marker = match.group(2)
            ordered = marker.endswith(".")
            items.append(_inline(match.group(3).strip()))
            cursor += 1
            continue
        if (
            items
            and line.strip()
            and not _HEADING_RE.match(line.strip())
            and not _FENCE_RE.match(line)
            and not _HR_RE.match(line.strip())
            and not line.lstrip().startswith(">")
            and "|" not in line
        ):
            # Lazy/indented continuation of the previous list item.
            items[-1] = f"{items[-1]} {_inline(line.strip())}"
            cursor += 1
            continue
        break
    tag = "ol" if ordered else "ul"
    body = "".join(f"<li>{item}</li>" for item in items)
    return f"<{tag}>{body}</{tag}>", cursor


def _render_blocks(lines: list[str]) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []
    index = 0

    def flush() -> None:
        if paragraph:
            blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            flush()
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            flush()
            marker = fence.group(1)
            language = fence.group(2)
            body: list[str] = []
            index += 1
            while index < len(lines):
                if lines[index].strip().startswith(marker):
                    index += 1
                    break
                body.append(lines[index])
                index += 1
            code = html.escape("\n".join(body))
            css = f' class="language-{html.escape(language)}"' if language else ""
            blocks.append(f"<pre><code{css}>{code}</code></pre>")
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        if _HR_RE.match(stripped):
            flush()
            blocks.append("<hr />")
            index += 1
            continue

        if stripped.startswith(">"):
            flush()
            quote: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                value = lines[index].strip()[1:]
                quote.append(value[1:] if value.startswith(" ") else value)
                index += 1
            blocks.append(f"<blockquote>{_render_blocks(quote)}</blockquote>")
            continue

        if _is_table(lines, index):
            flush()
            table, index = _render_table(lines, index)
            blocks.append(table)
            continue

        if _LIST_RE.match(line):
            flush()
            listing, index = _render_list(lines, index)
            blocks.append(listing)
            continue

        paragraph.append(stripped)
        index += 1

    flush()
    return "\n".join(blocks)


def render_markdown(text: str) -> str:
    """Convert the guide Markdown subset into QTextDocument-friendly HTML."""
    return _render_blocks(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def iter_links(text: str) -> Iterable[str]:
    """Yield Markdown link targets (used by tests and diagnostics)."""
    for match in _LINK_RE.finditer(text):
        yield match.group(2)
