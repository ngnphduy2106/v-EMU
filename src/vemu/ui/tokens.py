"""Clarity-inspired design tokens for the Qt desktop UI.

Layering follows the Clarity model, adapted for a desktop app:

* global scales (type, spacing, radius, control sizes, motion);
* semantic colors in :mod:`vemu.ui.theme` (background/surface/text/action/status);
* component QSS in :mod:`vemu.ui.theme` and widget helpers.

Global values are intentionally small and stable. Component-specific values
(such as page margins) live in ``COMPONENT`` so screens do not hard-code pixels.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TypeScale:
    micro: int = 10
    small: int = 11
    body_sm: int = 12
    body: int = 13
    body_lg: int = 15
    subtitle: int = 16
    section: int = 17
    title: int = 24
    hero: int = 18

    weight_regular: int = 400
    weight_medium: int = 500
    weight_semibold: int = 600
    weight_bold: int = 700


@dataclass(frozen=True)
class Space:
    xxs: int = 2
    xs: int = 4
    sm: int = 8
    md: int = 12
    lg: int = 16
    xl: int = 24
    xxl: int = 32


@dataclass(frozen=True)
class Radius:
    xs: int = 4
    sm: int = 6
    md: int = 7
    lg: int = 8
    xl: int = 10
    pill: int = 999


@dataclass(frozen=True)
class Control:
    height_sm: int = 28
    height_md: int = 32
    height_lg: int = 36
    padding_x: int = 8
    padding_y: int = 6
    button_padding_x: int = 12
    button_padding_y: int = 7
    table_row: int = 40
    table_header: int = 32


@dataclass(frozen=True)
class Motion:
    fast: int = 120
    base: int = 180
    slow: int = 240


@dataclass(frozen=True)
class Component:
    page_margin: int = 24
    page_top: int = 20
    page_bottom: int = 20
    section_gap: int = 12
    form_gap: int = 8
    dialog_width: int = 620
    confirm_width: int = 460
    progress_width: int = 420
    dialog_padding: int = 18


TYPE = TypeScale()
SPACE = Space()
RADIUS = Radius()
CONTROL = Control()
MOTION = Motion()
COMPONENT = Component()
