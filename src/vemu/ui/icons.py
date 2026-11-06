"""Small vector icons drawn with QPainter.

Text glyphs (``☰``, ``⚙``, ``⛶``…) render with different visual weights and
margins depending on the installed font, which makes a toolbar look uneven.
These icons are drawn on a shared 24×24 grid so every glyph has the same
optical size and stroke weight.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

try:  # Qt SVG is part of PySide6, but keep the QPainter fallback usable.
    from PySide6.QtSvg import QSvgRenderer
except ImportError:  # pragma: no cover - packaging fallback
    QSvgRenderer = None  # type: ignore[assignment]

GRID = 24.0
DEFAULT_SIZE = 18
DEFAULT_DPR = 2.0
ICON_ASSETS = Path(__file__).with_name("assets") / "icons"


def _pen(color: str, width: float = 1.7) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _gear_path() -> QPainterPath:
    center = 12.0
    outer = 8.6
    root = 6.1
    hole = 2.8
    teeth = 8
    step = 2 * math.pi / teeth
    points: list[QPointF] = []
    for index in range(teeth):
        base = index * step
        for offset, radius in (
            (-step * 0.30, root),
            (-step * 0.16, outer),
            (step * 0.16, outer),
            (step * 0.30, root),
        ):
            angle = base + offset
            points.append(
                QPointF(
                    center + radius * math.cos(angle),
                    center + radius * math.sin(angle),
                )
            )
    path = QPainterPath(points[0])
    for point in points[1:]:
        path.lineTo(point)
    path.closeSubpath()
    cutout = QPainterPath()
    cutout.addEllipse(QPointF(center, center), hole, hole)
    return path.subtracted(cutout)


def _draw(name: str, painter: QPainter, color: str) -> None:
    painter.setPen(_pen(color))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if name == "menu":
        for y in (6.5, 12.0, 17.5):
            painter.drawLine(QPointF(4.5, y), QPointF(19.5, y))
    elif name == "sun":
        painter.drawEllipse(QPointF(12, 12), 4.0, 4.0)
        for index in range(8):
            angle = math.radians(index * 45)
            painter.drawLine(
                QPointF(
                    12 + 6.6 * math.cos(angle),
                    12 + 6.6 * math.sin(angle),
                ),
                QPointF(
                    12 + 9.3 * math.cos(angle),
                    12 + 9.3 * math.sin(angle),
                ),
            )
    elif name == "moon":
        outer = QPainterPath()
        outer.addEllipse(QRectF(4.8, 4.6, 14.4, 14.8))
        inner = QPainterPath()
        inner.addEllipse(QRectF(9.9, 2.3, 14.4, 14.8))
        painter.fillPath(outer.subtracted(inner), QColor(color))
    elif name == "panel":
        painter.drawRoundedRect(QRectF(3.8, 5.0, 16.4, 14.0), 2.4, 2.4)
        painter.drawLine(QPointF(15.0, 5.0), QPointF(15.0, 19.0))
    elif name == "gear":
        painter.fillPath(_gear_path(), QColor(color))
    elif name == "topology":
        painter.setBrush(QColor(color))
        for x, y in ((12.0, 5.0), (5.0, 18.0), (19.0, 18.0)):
            painter.drawEllipse(QPointF(x, y), 2.4, 2.4)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(11.0, 7.0), QPointF(6.0, 15.8))
        painter.drawLine(QPointF(13.0, 7.0), QPointF(18.0, 15.8))
        painter.drawLine(QPointF(7.5, 18.0), QPointF(16.5, 18.0))
    elif name == "play":
        path = QPainterPath(QPointF(8.0, 5.0))
        path.lineTo(QPointF(19.0, 12.0))
        path.lineTo(QPointF(8.0, 19.0))
        path.closeSubpath()
        painter.fillPath(path, QColor(color))
    elif name == "power":
        painter.drawArc(QRectF(5.0, 5.0, 14.0, 14.0), 35 * 16, 290 * 16)
        painter.drawLine(QPointF(12.0, 3.5), QPointF(12.0, 10.0))
    elif name == "force":
        painter.drawRoundedRect(QRectF(5.5, 5.5, 13.0, 13.0), 1.0, 1.0)
        painter.drawLine(QPointF(7.0, 7.0), QPointF(17.0, 17.0))
        painter.drawLine(QPointF(17.0, 7.0), QPointF(7.0, 17.0))
    elif name == "snapshot":
        painter.drawRoundedRect(QRectF(3.5, 7.0, 17.0, 12.0), 2.0, 2.0)
        painter.drawRoundedRect(QRectF(8.0, 4.5, 8.0, 3.0), 1.0, 1.0)
        painter.drawEllipse(QPointF(12.0, 13.0), 3.4, 3.4)
    elif name == "network":
        painter.setBrush(QColor(color))
        for x, y in ((12.0, 5.0), (5.0, 18.0), (19.0, 18.0)):
            painter.drawEllipse(QPointF(x, y), 2.2, 2.2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(11.0, 7.0), QPointF(6.0, 15.8))
        painter.drawLine(QPointF(13.0, 7.0), QPointF(18.0, 15.8))
        painter.drawLine(QPointF(7.5, 18.0), QPointF(16.5, 18.0))
    elif name == "hardware":
        painter.drawRoundedRect(QRectF(5.5, 5.5, 13.0, 13.0), 2.0, 2.0)
        painter.drawRoundedRect(QRectF(9.5, 9.5, 5.0, 5.0), 1.0, 1.0)
        for offset in (8.0, 12.0, 16.0):
            painter.drawLine(QPointF(offset, 3.0), QPointF(offset, 5.0))
            painter.drawLine(QPointF(offset, 19.0), QPointF(offset, 21.0))
            painter.drawLine(QPointF(3.0, offset), QPointF(5.0, offset))
            painter.drawLine(QPointF(19.0, offset), QPointF(21.0, offset))
    elif name == "detach":
        painter.drawRoundedRect(QRectF(4.0, 7.0, 9.0, 11.0), 1.8, 1.8)
        painter.drawLine(QPointF(10.0, 12.0), QPointF(20.0, 12.0))
        painter.drawLine(QPointF(16.5, 8.5), QPointF(20.0, 12.0))
        painter.drawLine(QPointF(16.5, 15.5), QPointF(20.0, 12.0))
    elif name == "terminal":
        painter.drawRoundedRect(QRectF(3.5, 5.0, 17.0, 14.0), 2.2, 2.2)
        painter.drawLine(QPointF(7.0, 9.5), QPointF(10.5, 12.0))
        painter.drawLine(QPointF(10.5, 12.0), QPointF(7.0, 14.5))
        painter.drawLine(QPointF(12.5, 14.5), QPointF(17.0, 14.5))
    elif name == "fullscreen":
        for points in (
            ((4.5, 9.5), (4.5, 4.5), (9.5, 4.5)),
            ((14.5, 4.5), (19.5, 4.5), (19.5, 9.5)),
            ((19.5, 14.5), (19.5, 19.5), (14.5, 19.5)),
            ((9.5, 19.5), (4.5, 19.5), (4.5, 14.5)),
        ):
            path = QPainterPath(QPointF(*points[0]))
            path.lineTo(QPointF(*points[1]))
            path.lineTo(QPointF(*points[2]))
            painter.drawPath(path)
    elif name == "restore":
        for points in (
            ((9.0, 4.5), (9.0, 9.0), (4.5, 9.0)),
            ((15.0, 4.5), (15.0, 9.0), (19.5, 9.0)),
            ((19.5, 15.0), (15.0, 15.0), (15.0, 19.5)),
            ((4.5, 15.0), (9.0, 15.0), (9.0, 19.5)),
        ):
            path = QPainterPath(QPointF(*points[0]))
            path.lineTo(QPointF(*points[1]))
            path.lineTo(QPointF(*points[2]))
            painter.drawPath(path)
    else:  # pragma: no cover - programming error
        raise ValueError(f"Unknown icon: {name}")


def _svg_pixmap(
    name: str,
    color: str,
    size: int,
    dpr: float,
) -> QPixmap | None:
    if QSvgRenderer is None:
        return None
    path = ICON_ASSETS / f"{name}.svg"
    try:
        markup = path.read_text(encoding="utf-8")
    except OSError:
        return None
    markup = markup.replace("currentColor", color)
    root_end = markup.find(">")
    root = markup[:root_end]
    if "fill=" not in root:
        # Clarity assets rely on CSS classes for fill; QSvgRenderer does not
        # load those classes, so set an explicit default fill.
        markup = markup.replace("<svg", f'<svg fill="{color}"', 1)
    renderer = QSvgRenderer(QByteArray(markup.encode("utf-8")))
    if not renderer.isValid():
        return None
    pixmap = QPixmap(
        int(round(size * dpr)), int(round(size * dpr))
    )
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(dpr)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0.0, 0.0, float(size), float(size)))
    painter.end()
    return pixmap


def icon_pixmap(
    name: str,
    color: str,
    size: int = DEFAULT_SIZE,
    dpr: float = DEFAULT_DPR,
) -> QPixmap:
    svg = _svg_pixmap(name, color, size, dpr)
    if svg is not None:
        return svg
    pixmap = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(dpr)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(size / GRID, size / GRID)
    _draw(name, painter, color)
    painter.end()
    return pixmap


def make_icon(
    name: str,
    color: str,
    active_color: str | None = None,
    size: int = DEFAULT_SIZE,
    dpr: float = DEFAULT_DPR,
    disabled_color: str | None = None,
) -> QIcon:
    """Return a QIcon with hover/active and optional disabled variants."""

    icon = QIcon()
    normal = icon_pixmap(name, color, size, dpr)
    icon.addPixmap(normal, QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(normal, QIcon.Mode.Selected, QIcon.State.Off)
    if active_color is not None:
        active = icon_pixmap(name, active_color, size, dpr)
        icon.addPixmap(active, QIcon.Mode.Active, QIcon.State.Off)
        icon.addPixmap(active, QIcon.Mode.Selected, QIcon.State.On)
    if disabled_color is not None:
        disabled = icon_pixmap(name, disabled_color, size, dpr)
        icon.addPixmap(disabled, QIcon.Mode.Disabled, QIcon.State.Off)
    return icon
