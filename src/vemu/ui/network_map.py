"""Network topology visualization."""

from __future__ import annotations

import html
import math
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..errors import VemuError
from .dialogs import VemuDialog, dialog_layout
from .theme import DARK, LIGHT, Tokens

ASSETS = Path(__file__).parent / "assets" / "network_map"


def _count(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value == 1 else plural}"


class ZoomGraphicsView(QGraphicsView):
    """Network map view with explicit zoom controls and Ctrl+wheel zoom."""

    zoom_changed = Signal(int)

    MIN_ZOOM = 35
    MAX_ZOOM = 250

    def __init__(self, scene: QGraphicsScene, parent=None) -> None:
        super().__init__(scene, parent)
        self._zoom = 100
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @property
    def zoom_percent(self) -> int:
        return self._zoom

    def set_zoom(self, percent: int) -> None:
        target = max(self.MIN_ZOOM, min(self.MAX_ZOOM, int(percent)))
        if target == self._zoom:
            return
        factor = target / self._zoom
        self.scale(factor, factor)
        self._zoom = target
        self.zoom_changed.emit(target)

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom + 15)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom - 15)

    def reset_zoom(self) -> None:
        self.resetTransform()
        self._zoom = 100
        self.zoom_changed.emit(self._zoom)

    def fit(self) -> None:
        rect = self.sceneRect()
        if rect.isNull() or rect.isEmpty():
            return
        self.fitInView(
            rect.adjusted(-24, -20, 24, 20),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        self._zoom = max(
            1, round(self.transform().m11() * 100)
        )
        self.zoom_changed.emit(self._zoom)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() >= 0:
                self.zoom_in()
            else:
                self.zoom_out()
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        key = event.key()
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_in()
        elif key == Qt.Key.Key_Minus:
            self.zoom_out()
        elif key == Qt.Key.Key_0:
            self.reset_zoom()
        elif key == Qt.Key.Key_F:
            self.fit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()


class NetworkMapDialog(VemuDialog):
    def __init__(self, backend, *, dark: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.dark = dark
        self.tokens = DARK if dark else LIGHT
        self.payload: dict = {}
        self.setWindowTitle("Network map")
        self.resize(1080, 640)

        layout = dialog_layout(self)

        header = QHBoxLayout()
        title = QLabel("Network map")
        title.setObjectName("DialogTitle")
        header.addWidget(title)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("Muted")
        header.addWidget(self.summary_label)
        header.addStretch(1)
        self.zoom_out_button = self._tool_button("−", "Zoom out", self.view_zoom_out)
        header.addWidget(self.zoom_out_button)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("Muted")
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setFixedWidth(50)
        header.addWidget(self.zoom_label)
        self.zoom_in_button = self._tool_button("+", "Zoom in", self.view_zoom_in)
        header.addWidget(self.zoom_in_button)
        self.fit_button = self._tool_button(
            "Fit", "Fit the map in the window", self.view_fit
        )
        header.addWidget(self.fit_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("PrimaryButton")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        close = QPushButton("Close")
        close.setObjectName("SecondaryButton")
        close.clicked.connect(self.accept)
        header.addWidget(close)
        layout.addLayout(header)

        legend = QHBoxLayout()
        legend.setSpacing(6)
        for name, label in (
            ("host", "Hypervisor host"),
            ("switch", "Virtual switch"),
            ("guest", "Guest / node"),
        ):
            icon = QLabel()
            pixmap = self._icon(name, 18)
            if pixmap is not None:
                icon.setPixmap(pixmap)
                icon.setFixedSize(18, 18)
            legend.addWidget(icon)
            text = QLabel(label)
            text.setObjectName("Muted")
            legend.addWidget(text)
            if name != "guest":
                spacer = QLabel("·")
                spacer.setObjectName("Muted")
                legend.addWidget(spacer)
        legend.addStretch(1)
        hint = QLabel("Ctrl+wheel zooms · drag to pan")
        hint.setObjectName("Muted")
        legend.addWidget(hint)
        layout.addLayout(legend)

        self.scene = QGraphicsScene(self)
        self.view = ZoomGraphicsView(self.scene, self)
        self.view.zoom_changed.connect(self._zoom_changed)
        self.view.setBackgroundBrush(QColor(self.tokens.bg))
        self._fit_timer = QTimer(self.view)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self.view.fit)
        layout.addWidget(self.view, 1)

        self.refresh()

    def _tool_button(self, text: str, tooltip: str, callback) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("SecondaryButton")
        button.setToolTip(tooltip)
        button.setFixedSize(34, 30)
        button.clicked.connect(callback)
        return button

    def _zoom_changed(self, percent: int) -> None:
        self.zoom_label.setText(f"{percent}%")

    def view_zoom_in(self) -> None:
        self.view.zoom_in()

    def view_zoom_out(self) -> None:
        self.view.zoom_out()

    def view_fit(self) -> None:
        self.view.fit()

    def refresh(self) -> None:
        try:
            if hasattr(self.backend, "network_topology"):
                self.payload = self.backend.network_topology()
            else:
                from ..service import network_topology

                self.payload = network_topology()
        except VemuError as exc:
            self.payload = {
                "host": {"name": "host"},
                "networks": [],
                "vms": [],
                "warnings": [str(exc)],
            }
        self._draw()

    def _icon(self, name: str, size: int = 38) -> QPixmap | None:
        path = ASSETS / f"{name}.svg"
        try:
            markup = path.read_text(encoding="utf-8")
        except OSError:
            return None
        tokens = self.tokens
        replacements = {
            "%BG%": tokens.bg,
            "%SURFACE%": tokens.surface,
            "%ACCENT%": tokens.accent,
            "%MUTED%": tokens.muted,
            "%TEXT%": tokens.text,
            "%BORDER_STRONG%": tokens.border_strong,
        }
        for placeholder, value in replacements.items():
            markup = markup.replace(placeholder, value)
        renderer = QSvgRenderer(QByteArray(markup.encode("utf-8")))
        if not renderer.isValid():
            return None
        dpr = 2.0
        pixmap = QPixmap(int(size * dpr), int(size * dpr))
        pixmap.fill(Qt.GlobalColor.transparent)
        pixmap.setDevicePixelRatio(dpr)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return pixmap

    def _draw(self) -> None:
        self.scene.clear()
        self.tokens = DARK if self.dark else LIGHT
        self.view.setBackgroundBrush(QColor(self.tokens.bg))
        tokens = self.tokens
        networks = list(self.payload.get("networks", []))
        vms = list(self.payload.get("vms", []))
        known = {str(net.get("name", "")) for net in networks}
        groups: dict[str, list[dict]] = {}
        for vm in vms:
            interfaces = vm.get("interfaces") or []
            for interface in interfaces:
                name = str(interface.get("network", ""))
                if name and name not in known:
                    networks.append({"name": name, "mode": "external"})
                    known.add(name)
            first = (
                str(interfaces[0].get("network", ""))
                if interfaces
                else "unassigned"
            )
            groups.setdefault(first, []).append(vm)
        if "unassigned" in groups:
            networks.append({"name": "unassigned", "mode": "none"})
            known.add("unassigned")

        self.summary_label.setText(
            f"{_count(len(networks), 'switch', 'switches')} · "
            f"{_count(len(vms), 'guest', 'guests')}"
        )
        if not networks:
            width, height = 960, 620
            self.scene.setSceneRect(0, 0, width, height)
            self._host_card(
                width / 2 - 160,
                24,
                320,
                _count(len(vms), "guest", "guests"),
                tokens,
            )
            text = self.scene.addText(
                "No networks or running VMs to display."
            )
            text.setDefaultTextColor(QColor(tokens.muted))
            text.setPos(40, 180)
            self._finish_layout(width, height)
            return

        card_w = 240
        gap = 38
        margin = 40
        vm_w = 206
        vm_gap = 14
        max_cols = 5
        network_lines = {
            str(net.get("name", "")): self._network_lines(net)
            for net in networks
        }
        network_heights = {
            name: self._card_height(lines)
            for name, lines in network_lines.items()
        }
        max_net_h = max(network_heights.values(), default=84)
        max_vm_h = max(
            (
                self._card_height(self._vm_lines(vm))
                for group in groups.values()
                for vm in group
            ),
            default=84,
        )
        vm_step = max_vm_h + 16

        grids: dict[str, tuple[int, int, float]] = {}
        column_widths: list[float] = []
        max_rows = 0
        for network in networks:
            name = str(network.get("name", ""))
            count = len(groups.get(name, []))
            cols = min(max_cols, max(1, math.ceil(count / 3))) if count else 1
            rows = math.ceil(count / cols) if count else 0
            grid_w = cols * vm_w + (cols - 1) * vm_gap
            col_w = max(card_w, grid_w)
            grids[name] = (cols, rows, grid_w)
            column_widths.append(col_w)
            max_rows = max(max_rows, rows)
        total_w = sum(column_widths) + (len(networks) - 1) * gap
        width = max(960, margin * 2 + total_w)
        start_x = (width - total_w) / 2
        net_y = 166
        vm_y = net_y + max_net_h + 78
        content_bottom = vm_y + max_rows * vm_step
        warnings = self.payload.get("warnings") or []
        warning_height = 24 * len(warnings)
        height = max(640, content_bottom + 56 + warning_height)

        host_w = 330
        host_h = 96
        host_x = width / 2 - host_w / 2
        self._host_card(
            host_x,
            24,
            host_w,
            _count(len(networks), "switch", "switches"),
            tokens,
        )
        host_bottom = 24 + host_h
        pen = QPen(QColor(tokens.border_strong))
        pen.setWidth(2)
        pen_secondary = QPen(QColor(tokens.border))
        pen_secondary.setWidth(2)
        pen_secondary.setStyle(Qt.PenStyle.DashLine)

        positions: dict[str, tuple[float, float, float, float, float, float]] = {}
        offset = start_x
        for index, network in enumerate(networks):
            name = str(network.get("name", ""))
            col_w = column_widths[index]
            net_x = offset + (col_w - card_w) / 2
            box_height = network_heights[name]
            self._card(
                net_x,
                net_y,
                card_w,
                box_height,
                "switch",
                name,
                network_lines[name],
                tokens.accent_subtle,
                tokens,
                online=bool((network.get("runtime") or {}).get("active", False)),
            )
            positions[name] = (
                net_x,
                net_y,
                card_w,
                box_height,
                offset,
                col_w,
            )
            self._connect(
                (width / 2, host_bottom),
                (net_x + card_w / 2, net_y),
                pen,
            )
            offset += col_w + gap

        for name, group in groups.items():
            group.sort(
                key=lambda vm: (
                    vm.get("state") != "running",
                    str(vm.get("name", "")).lower(),
                )
            )
            net_x, y, _w, h, col_x, col_w = positions[name]
            cols, _rows, grid_w = grids[name]
            group_x = col_x + (col_w - grid_w) / 2
            for index, vm in enumerate(group):
                row = index // cols
                column = index % cols
                vm_x = group_x + column * (vm_w + vm_gap)
                vy = vm_y + row * vm_step
                vm_h = self._card_height(self._vm_lines(vm))
                self._card(
                    vm_x,
                    vy,
                    vm_w,
                    vm_h,
                    "guest",
                    str(vm.get("name", "")),
                    self._vm_lines(vm),
                    tokens.surface,
                    tokens,
                    online=str(vm.get("state", "")) == "running",
                )
                self._connect(
                    (net_x + card_w / 2, y + h),
                    (vm_x + vm_w / 2, vy),
                    pen,
                )
                for interface in vm.get("interfaces") or []:
                    other = str(interface.get("network", ""))
                    if other and other != name and other in positions:
                        ox, oy, ow, oh, _ox, _ow = positions[other]
                        self._connect(
                            (ox + ow / 2, oy + oh),
                            (vm_x + vm_w / 2, vy),
                            pen_secondary,
                        )

        if warnings:
            text = self.scene.addText(
                "\n".join(str(item) for item in warnings)
            )
            text.setDefaultTextColor(QColor(tokens.warning))
            text.setTextWidth(width - margin * 2)
            text.setPos(margin, height - warning_height - 20)

        self._finish_layout(width, height)

    def _finish_layout(self, width: float, height: float) -> None:
        self.scene.setSceneRect(0, 0, width, height)
        self._fit_timer.start(0)

    def _host_card(
        self, x: float, y: float, width: float, subtitle: str, tokens: Tokens
    ) -> None:
        host = str(self.payload.get("host", {}).get("name", "host"))
        self._card(
            x,
            y,
            width,
            96,
            "host",
            f"Host · {host}",
            ["QEMU/KVM hypervisor", subtitle],
            tokens.accent_subtle,
            tokens,
            online=True,
        )

    def _network_lines(self, network: dict) -> list[str]:
        forwards = network.get("port_forwards") or []
        reservations = network.get("host_reservations") or []
        runtime = network.get("runtime") or {}
        active = "active" if runtime.get("active", False) else "inactive"
        bridge = network.get("bridge", "") or "—"
        lines = [
            f"{network.get('mode', '')} · {network.get('subnet', 'auto')}",
            f"{active} · bridge {bridge}",
        ]
        for forward in forwards[:4]:
            lines.append(
                f"{forward.get('protocol', 'tcp')}/"
                f"{forward.get('host_port')} → "
                f"{forward.get('guest_ip')}:{forward.get('guest_port')}"
            )
        if len(forwards) > 4:
            lines.append(f"+{len(forwards) - 4} more forward(s)")
        if reservations:
            lines.append(f"{len(reservations)} reservation(s)")
        return lines

    def _vm_lines(self, vm: dict) -> list[str]:
        state = str(vm.get("state", ""))
        lines = [state]
        ips = []
        for interface in vm.get("interfaces") or []:
            for ip in interface.get("ips") or []:
                source = interface.get("ip_source", "")
                ips.append(f"{ip} ({source})" if source else ip)
        if ips:
            lines.extend(ips[:3])
        else:
            lines.append("no guest IP")
        return lines

    @staticmethod
    def _card_height(lines: list[str]) -> float:
        return max(84, 56 + 16 * len(lines) + 12)

    def _card(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        icon_name: str,
        title: str,
        lines: list[str],
        fill: str,
        tokens: Tokens,
        *,
        online: bool | None = None,
    ) -> None:
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, width, height), 9, 9)
        card = self.scene.addPath(
            path,
            QPen(QColor(tokens.border), 1.0),
            QBrush(QColor(fill)),
        )
        card.setZValue(1)

        pixmap = self._icon(icon_name, 38)
        if pixmap is not None:
            icon = QGraphicsPixmapItem(pixmap)
            icon.setPos(x + 14, y + 12)
            icon.setZValue(2)
            self.scene.addItem(icon)

        if online is not None:
            color = tokens.success if online else tokens.muted
            dot = self.scene.addEllipse(
                x + 44,
                y + 10,
                11,
                11,
                QPen(QColor(fill), 2.0),
                QBrush(QColor(color)),
            )
            dot.setZValue(4)
            dot.setToolTip("Online" if online else "Offline")

        self._text(
            x + 62,
            y + 9,
            width - 72,
            f"<b>{html.escape(title)}</b>",
            tokens.text,
            13,
        )
        if lines:
            body = "<br>".join(html.escape(str(line)) for line in lines)
            self._text(
                x + 14,
                y + 54,
                width - 28,
                body,
                tokens.muted,
                11,
            )

    def _text(
        self,
        x: float,
        y: float,
        width: float,
        markup: str,
        color: str,
        size: int,
    ) -> QGraphicsTextItem:
        item = QGraphicsTextItem()
        item.setDefaultTextColor(QColor(color))
        item.setHtml(
            f"<div style='font-size:{size}px; line-height:1.18;'>{markup}</div>"
        )
        item.setTextWidth(width)
        item.document().setDocumentMargin(0)
        item.setPos(x, y)
        item.setZValue(3)
        self.scene.addItem(item)
        return item

    def _connect(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        pen: QPen,
    ) -> None:
        line = self.scene.addLine(
            start[0], start[1], end[0], end[1], pen
        )
        line.setZValue(0)
