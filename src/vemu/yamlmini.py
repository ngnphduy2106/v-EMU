"""Small YAML subset loader for v-EMU lab files.

The project intentionally has no runtime YAML dependency. Lab specs only need
mappings, sequences, inline `{...}`/`[...]` values and scalars, so this loader
covers that subset and fails loudly on anything else.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$")


class YAMLError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _split_top(text: str, separator: str = ",") -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote == '"':
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if "".join(current).strip() or parts:
        parts.append("".join(current).strip())
    return parts


def _split_key(text: str) -> tuple[str, str] | None:
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == ":" and (
            index + 1 == len(text) or text[index + 1].isspace()
        ):
            key = text[:index].strip()
            if not key:
                return None
            return key, text[index + 1 :].strip()
    return None


def _scalar(text: str) -> Any:
    value = text.strip()
    if not value:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _split_top(inner)]
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        if not inner:
            return {}
        result: dict[str, Any] = {}
        for part in _split_top(inner):
            pair = _split_key(part)
            if pair is None:
                raise YAMLError(f"Invalid inline mapping entry: {part!r}")
            key, raw = pair
            result[str(_scalar(key))] = _scalar(raw)
        return result
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError) as exc:
            raise YAMLError(f"Invalid quoted string: {value}") from exc
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if INT_RE.match(value):
        return int(value)
    if FLOAT_RE.match(value):
        return float(value)
    return value


def _parse_block(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[Any, int]:
    if lines[index][1].startswith("-"):
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_map(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise YAMLError(f"Unexpected indentation near: {text!r}")
        if text.startswith("-"):
            break
        pair = _split_key(text)
        if pair is None:
            raise YAMLError(f"Expected 'key: value', got: {text!r}")
        key, raw = pair
        index += 1
        if raw:
            result[key] = _scalar(raw)
            continue
        if index < len(lines) and lines[index][0] > indent:
            result[key], index = _parse_block(
                lines, index, lines[index][0]
            )
        else:
            result[key] = None
    return result, index


def _parse_list(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise YAMLError(f"Unexpected indentation near: {text!r}")
        if not text.startswith("-"):
            break
        content = text[1:].lstrip()
        if not content:
            index += 1
            if index < len(lines) and lines[index][0] > indent:
                item, index = _parse_block(
                    lines, index, lines[index][0]
                )
            else:
                item = None
            result.append(item)
            continue
        if _split_key(content) is not None:
            item_lines: list[tuple[int, str]] = [
                (indent + 2, content)
            ]
            index += 1
            while index < len(lines) and lines[index][0] > indent:
                item_lines.append(lines[index])
                index += 1
            item, _consumed = _parse_map(item_lines, 0, indent + 2)
            result.append(item)
            continue
        result.append(_scalar(content))
        index += 1
    return result, index


def loads(text: str) -> Any:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in {"---", "..."}:
            continue
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        lines.append((indent, line.strip()))
    if not lines:
        return None
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise YAMLError(
            f"Unexpected content at line: {lines[index][1]!r}"
        )
    return value


def load(path: str | Path) -> Any:
    return loads(Path(path).expanduser().read_text(encoding="utf-8"))


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _dump_key(key: str) -> str:
    if re.match(r"^[A-Za-z0-9_.-]+$", key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _dump(value: Any, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            key_text = _dump_key(str(key))
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{key_text}:")
                lines.extend(_dump(item, indent + 2))
            else:
                lines.append(f"{pad}{key_text}: {_dump_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{pad}- {{}}")
                    continue
                first = True
                for key, entry in item.items():
                    key_text = _dump_key(str(key))
                    prefix = f"{pad}- " if first else f"{pad}  "
                    if isinstance(entry, (dict, list)):
                        lines.append(f"{prefix}{key_text}:")
                        lines.extend(_dump(entry, indent + 4))
                    else:
                        lines.append(
                            f"{prefix}{key_text}: {_dump_scalar(entry)}"
                        )
                    first = False
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.extend(_dump(item, indent + 2))
            else:
                lines.append(f"{pad}- {_dump_scalar(item)}")
        return lines
    raise YAMLError("Top-level YAML value must be a mapping or list.")


def dumps(value: Any) -> str:
    if not isinstance(value, (dict, list)):
        raise YAMLError("Top-level YAML value must be a mapping or list.")
    return "\n".join(_dump(value, 0)) + "\n"

