"""Clone name patterns.

The only supported token is ``{{n}}``. It is replaced by the clone index.
All user-facing messages are English.
"""

from __future__ import annotations

from .errors import ValidationError

TOKEN = "{{n}}"


def validate_pattern(pattern: str) -> str:
    """Validate a clone name pattern and return it.

    A pattern may contain at most one ``{{n}}`` token. A pattern without the
    token is allowed only when exactly one clone is requested; this is checked
    by :func:`render_names`.
    """
    if not pattern or not pattern.strip():
        raise ValidationError("Name pattern must not be empty.")
    if pattern.count(TOKEN) > 1:
        raise ValidationError(
            f"Name pattern may contain at most one {TOKEN} token."
        )
    if "/" in pattern or "\\" in pattern:
        raise ValidationError("Name pattern must not contain path separators.")
    if pattern in {".", ".."}:
        raise ValidationError("Name pattern is not a valid name.")
    return pattern


def render(pattern: str, index: int) -> str:
    """Render a pattern for one clone index."""
    validate_pattern(pattern)
    return pattern.replace(TOKEN, str(index))


def render_names(
    pattern: str,
    count: int,
    start: int = 1,
    existing: set[str] | None = None,
) -> list[str]:
    """Render ``count`` names and reject duplicates or collisions."""
    validate_pattern(pattern)
    if count < 1:
        raise ValidationError("Count must be at least 1.")
    if count > 100:
        raise ValidationError("Count must be at most 100.")
    if start < 0:
        raise ValidationError("Start index must be zero or greater.")
    if TOKEN not in pattern and count > 1:
        raise ValidationError(
            f"Name pattern must contain {TOKEN} when cloning more than one VM."
        )

    names = [render(pattern, start + offset) for offset in range(count)]

    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        listed = ", ".join(sorted(duplicates))
        raise ValidationError(f"Name pattern produces duplicate names: {listed}.")

    existing = existing or set()
    collisions = sorted(set(names) & existing)
    if collisions:
        listed = ", ".join(collisions)
        raise ValidationError(f"These names already exist: {listed}.")

    return names
