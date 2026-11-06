"""Embedded noVNC consoles (tabbed).

noVNC is vendored under ``ui/web/novnc`` and served from a loopback-only HTTP
server so QtWebEngine can load it without ``file://`` restrictions. Each tab
owns one HTTP server and one WebSocket connection to QEMU's VNC server.
"""

from __future__ import annotations

import base64
import codecs
import fcntl
import json
import os
import pty
import shutil
import struct
import subprocess
import termios
import threading
import time
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import (
    QEvent,
    QObject,
    QSettings,
    QSize,
    QSocketNotifier,
    QStandardPaths,
    Qt,
    QThread,
    Slot,
    QTimer,
    QUrl,
    QUrlQuery,
    Signal,
)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtGui import QAction, QKeySequence, QPixmap, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .icons import make_icon
from .notify import notify
from .theme import DARK, LIGHT
from .tooltips import attach_tooltip, compact_tooltip

KEY_CTRL = 0xFFE3
KEY_ALT = 0xFFE9
KEY_DELETE = 0xFFFF
KEY_BACKSPACE = 0xFF08
KEY_PRINT = 0xFF61
KEY_F = {
    1: 0xFFBE,
    2: 0xFFBF,
    3: 0xFFC0,
    4: 0xFFC1,
    5: 0xFFC2,
    6: 0xFFC3,
    7: 0xFFC4,
    8: 0xFFC5,
    9: 0xFFC6,
    10: 0xFFC7,
    11: 0xFFC8,
    12: 0xFFC9,
}


def save_console_screenshot(
    data_url: str,
    domain: str,
    directory: Path | None = None,
) -> Path:
    if "," not in data_url:
        raise ValueError("Screenshot data URL is malformed.")
    _header, encoded = data_url.split(",", 1)
    data = base64.b64decode(encoded)
    if not data:
        raise ValueError("Screenshot is empty.")
    if directory is None:
        pictures = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.PicturesLocation
        )
        directory = Path(pictures or (Path.home() / "Pictures")) / "Screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"vemu-{domain}-{stamp}.jpg"
    counter = 2
    while path.exists():
        path = directory / f"vemu-{domain}-{stamp}-{counter}.jpg"
        counter += 1
    path.write_bytes(data)
    return path


def find_novnc_dir() -> Path:
    candidates: list[Path] = []
    override = os.environ.get("VEMU_NOVNC_DIR")
    if override:
        candidates.append(Path(override))
    candidates.append(Path(__file__).parent / "web" / "novnc")
    candidates.append(Path("/usr/share/novnc"))
    for candidate in candidates:
        if (candidate / "vnc.html").is_file():
            return candidate
    raise FileNotFoundError(
        "noVNC assets were not found. Set VEMU_NOVNC_DIR or install the novnc package."
    )


DEFAULT_QUALITY = 6
DEFAULT_COMPRESSION = 2
LOW_LATENCY_QUALITY = 5
LOW_LATENCY_COMPRESSION = 0


def console_acceleration_enabled() -> bool:
    """Return True when the host opts into the WebEngine GPU path.

    The global ``configure_webengine_environment()`` disables Chromium GPU
    unless ``VEMU_WEBENGINE_GPU=1`` is set. Keep the canvas in sync with that
    decision instead of forcing accelerated painting while GPU is disabled.
    """
    return os.environ.get("VEMU_WEBENGINE_GPU") == "1"


def console_url(
    info: dict[str, Any],
    http_port: int,
    *,
    view_only: bool = False,
    quality: int = DEFAULT_QUALITY,
    compression: int = DEFAULT_COMPRESSION,
    show_dot: bool = False,
) -> QUrl:
    url = QUrl(f"http://127.0.0.1:{http_port}/vnc.html")
    query = QUrlQuery()
    query.addQueryItem("autoconnect", "1")
    query.addQueryItem("resize", "scale")
    query.addQueryItem("reconnect", "1")
    query.addQueryItem("bell", "off")
    query.addQueryItem("quality", str(quality))
    query.addQueryItem("compression", str(compression))
    query.addQueryItem("host", str(info.get("host", "127.0.0.1")))
    query.addQueryItem("port", str(info.get("websocket")))
    query.addQueryItem("path", "")
    query.addQueryItem("encrypt", "0")
    if show_dot:
        query.addQueryItem("show_dot", "1")
    if view_only:
        query.addQueryItem("view_only", "1")
    password = info.get("password")
    if password:
        query.addQueryItem("password", str(password))
    url.setQuery(query)
    return url


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return None


class ConsoleServer:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> int:
        handler = partial(_QuietHandler, directory=str(self.directory))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()
        return int(self.httpd.server_address[1])

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


class _MiniOverlay(QWidget):
    """Transparent click target layered over the preview."""

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("MiniConsoleOverlay")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Open the full console")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)
        self.chip = QLabel("Open console ↗", self)
        self.chip.setObjectName("MiniConsoleChip")
        self.chip.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.chip.adjustSize()
        self.chip.move(
            max(0, self.width() - self.chip.width() - 8),
            max(0, self.height() - self.chip.height() - 8),
        )
        super().resizeEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class MiniConsole(QWidget):
    """Read-only screenshot preview for the selected VM.

    Deliberately avoids QWebEngine: embedding noVNC in the inspector caused
    Chromium compositor flashes on virtio-gpu/KDE. A periodic libvirt
    screenshot is enough for a preview; clicking opens the full console.
    """

    opened = Signal()

    def __init__(self, parent=None, backend=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.setObjectName("MiniConsole")
        self.setFixedHeight(150)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.domain: str | None = None
        self._pixmap: QPixmap | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._refresh_preview)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(0)

        self.placeholder = QLabel("Select a VM to preview its console.")
        self.placeholder.setObjectName("Muted")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self._layout.addWidget(self.placeholder, 1)

        self.preview = QLabel()
        self.preview.setObjectName("MiniConsolePreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.preview.hide()
        self._layout.addWidget(self.preview, 1)

        self.overlay = _MiniOverlay(self)
        self.overlay.clicked.connect(self.opened)
        self.overlay.hide()

    def set_theme(self, tokens) -> None:
        self.preview.setStyleSheet(
            f"background: #000; color: {tokens.muted};"
            f"border: 1px solid {tokens.accent}; border-radius: 6px;"
        )

    def load(self, info: dict[str, Any]) -> None:
        domain = str(info.get("domain", ""))
        if self.domain == domain and self._pixmap is not None:
            return
        self.clear()
        self.domain = domain or None
        if not self.domain:
            return
        self.placeholder.hide()
        self.preview.setText("Loading preview…")
        self.preview.show()
        self.overlay.raise_()
        self.overlay.show()
        self._refresh_preview()
        self._timer.start()

    def show_no_signal(self, domain: str | None = None) -> None:
        """Show a stopped VM in the preview without polling screenshots."""
        self._timer.stop()
        if domain:
            self.domain = domain
        self._pixmap = None
        self.placeholder.hide()
        self.preview.setPixmap(QPixmap())
        self.preview.setText("No signal")
        self.preview.setToolTip(
            "No console signal: the VM is stopped or the screenshot failed."
        )
        self.preview.show()
        self.overlay.hide()

    def _refresh_preview(self) -> None:
        if not self.domain:
            return
        try:
            if self.backend is not None and hasattr(self.backend, "screenshot"):
                data = self.backend.screenshot(self.domain)
            else:
                import base64

                from ..service import domain_screenshot

                payload = domain_screenshot(self.domain)
                data = base64.b64decode(str(payload.get("data", "")))
            pixmap = QPixmap()
            if not pixmap.loadFromData(data):
                raise ValueError("unsupported screenshot data")
            self._pixmap = pixmap
            self.preview.setText("")
            self.preview.setToolTip("")
            self._rescale_preview()
        except Exception as exc:  # noqa: BLE001 - show preview failure
            self._pixmap = None
            self.preview.setPixmap(QPixmap())
            self.preview.setText("No signal")
            self.preview.setToolTip(f"Preview unavailable: {exc}")

    def _rescale_preview(self) -> None:
        if self._pixmap is None:
            return
        width = max(1, self.preview.width())
        height = max(1, self.preview.height())
        self.preview.setPixmap(
            self._pixmap.scaled(
                width,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def clear(self) -> None:
        self._timer.stop()
        self.domain = None
        self._pixmap = None
        self.overlay.hide()
        self.preview.clear()
        self.preview.hide()
        self.placeholder.setText(
            "Select a VM to preview its console."
        )
        self.placeholder.show()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.overlay.setGeometry(self.rect())
        self.overlay.raise_()
        self._rescale_preview()
        super().resizeEvent(event)

    def shutdown(self) -> None:
        self.clear()


class ConsoleTab(QWidget):
    connection_changed = Signal(str)
    frame_size_changed = Signal(int, int)
    clipboard_received = Signal(str)

    def __init__(
        self,
        info: dict[str, Any],
        parent=None,
        *,
        low_latency: bool = False,
        pointer_dot: bool = False,
    ) -> None:
        super().__init__(parent)
        self.domain_name = str(info.get("domain", "VM"))
        self.info = info
        self.low_latency = bool(low_latency)
        self.pointer_dot = bool(pointer_dot)
        self._previous_maximized = False
        self._last_status = ""
        self._last_frame = (0, 0)
        self._clipboard_seq = 0
        self._disconnected_since = None
        self._reconnect_stopped = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.server = ConsoleServer(find_novnc_dir())
        http_port = self.server.start()
        self.view = QWebEngineView(self)
        layout.addWidget(self.view)

        settings = self.view.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptCanPaste, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.WebGLEnabled, False
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled,
            console_acceleration_enabled(),
        )
        self.view.page().featurePermissionRequested.connect(self._permission)
        self.view.page().fullScreenRequested.connect(self._full_screen)
        self.view.loadFinished.connect(self._page_loaded)
        self.view.setUrl(
            console_url(
                info,
                http_port,
                quality=(
                    LOW_LATENCY_QUALITY
                    if self.low_latency
                    else DEFAULT_QUALITY
                ),
                compression=(
                    LOW_LATENCY_COMPRESSION
                    if self.low_latency
                    else DEFAULT_COMPRESSION
                ),
                show_dot=self.pointer_dot,
            )
        )
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._poll_status)
        self._status_timer.start()
        # Clipboard updates are push-only from noVNC; polling is small and
        # keeps the Qt toolbar independent of WebChannel plumbing.
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.setInterval(500)
        self._clipboard_timer.timeout.connect(self._poll_clipboard)
        self._clipboard_timer.start()

    def _full_screen(self, request) -> None:
        request.accept()
        window = self.window()
        if request.toggleOn():
            self._previous_maximized = window.isMaximized()
            window.showFullScreen()
        elif self._previous_maximized:
            window.showMaximized()
        else:
            window.showNormal()

    def _permission(self, origin: QUrl, feature) -> None:
        granted = feature in {
            QWebEnginePage.Feature.ClipboardReadWrite,
            QWebEnginePage.Feature.ClipboardSanitizedWrite,
        }
        policy = (
            QWebEnginePage.PermissionPolicy.PermissionGrantedByUser
            if granted
            else QWebEnginePage.PermissionPolicy.PermissionDeniedByUser
        )
        self.view.page().setFeaturePermission(origin, feature, policy)

    def _run_js(self, script: str, callback=None) -> None:
        if callback is None:
            self.view.page().runJavaScript(script)
        else:
            self.view.page().runJavaScript(script, callback)

    def _page_loaded(self, ok: bool) -> None:
        if not ok:
            return
        self._clipboard_seq = 0
        # The Qt window owns the toolbar; hide noVNC's in-page control bar
        # and status banner so the guest fills the window.
        self._run_js(
            "(() => {"
            " if (document.getElementById('vemu-native-chrome')) return;"
            " const style = document.createElement('style');"
            " style.id = 'vemu-native-chrome';"
            " style.textContent = '#noVNC_control_bar_anchor,"
            " #noVNC_control_bar, #noVNC_control_bar_hint,"
            " #noVNC_status { display: none !important; }';"
            " document.head.appendChild(style);"
            " return 'ok';"
            "})()"
        )

    def _rfb_action(self, code: str, callback=None) -> None:
        self._run_js(
            "(() => { const ui = window.vemuUI; "
            "if (!ui || !ui.rfb) return 'no-rfb'; "
            "const rfb = ui.rfb; "
            f"{code} return 'ok'; }})()",
            callback,
        )

    def send_ctrl_alt_del(self) -> None:
        self._rfb_action("rfb.sendCtrlAltDel();")

    def send_key_chord(self, keys: list[tuple[int, str]]) -> None:
        events = [
            f"rfb.sendKey({keysym}, {json.dumps(code)}, true);"
            for keysym, code in keys
        ]
        events.extend(
            f"rfb.sendKey({keysym}, {json.dumps(code)}, false);"
            for keysym, code in reversed(keys)
        )
        self._rfb_action("".join(events))

    def paste_text(self, text: str) -> None:
        self._rfb_action(f"rfb.clipboardPasteFrom({json.dumps(text)});")

    def set_low_latency(self, enabled: bool) -> None:
        self.low_latency = bool(enabled)
        quality = (
            LOW_LATENCY_QUALITY if self.low_latency else DEFAULT_QUALITY
        )
        compression = (
            LOW_LATENCY_COMPRESSION
            if self.low_latency
            else DEFAULT_COMPRESSION
        )
        self._rfb_action(
            f"rfb.qualityLevel = {quality};"
            f"rfb.compressionLevel = {compression};"
        )

    def set_pointer_dot(self, enabled: bool) -> None:
        self.pointer_dot = bool(enabled)
        self._run_js(
            "(() => { if (!window.vemuSetShowDot) return 'no-bridge'; "
            f"window.vemuSetShowDot({'true' if self.pointer_dot else 'false'}); "
            "return 'ok'; })()"
        )

    def _poll_clipboard(self) -> None:
        self._run_js(
            "(() => {"
            " const ui = window.vemuUI;"
            " const rfb = ui && ui.rfb;"
            " if (!rfb) return JSON.stringify([0, null]);"
            " if (window.vemuClipboardRfb !== rfb) {"
            "   window.vemuClipboardRfb = rfb;"
            "   window.vemuClipboardText = null;"
            "   window.vemuClipboardSeq = 0;"
            "   rfb.addEventListener('clipboard', (event) => {"
            "     const detail = event && event.detail;"
            "     const text = detail ? detail.text : '';"
            "     window.vemuClipboardText ="
            "       typeof text === 'string' ? text : '';"
            "     window.vemuClipboardSeq ="
            "       (window.vemuClipboardSeq || 0) + 1;"
            "   });"
            " }"
            " return JSON.stringify(["
            "   window.vemuClipboardSeq || 0,"
            "   window.vemuClipboardText"
            " ]);"
            "})()",
            self._set_clipboard,
        )

    def _set_clipboard(self, payload: object) -> None:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return
        if not isinstance(payload, (list, tuple)) or len(payload) != 2:
            return
        try:
            sequence = int(payload[0])
        except (TypeError, ValueError):
            return
        text = payload[1]
        if sequence < self._clipboard_seq:
            # noVNC reconnected and reset its local sequence.
            self._clipboard_seq = 0
        if sequence <= self._clipboard_seq or not isinstance(text, str):
            return
        self._clipboard_seq = sequence
        if text:
            self.clipboard_received.emit(text)

    def set_scale(self, mode: str) -> None:
        setting = {
            "fit": "scale",
            "actual": "off",
        }.get(mode, "scale")
        self._run_js(
            "(() => { if (!window.vemuSetResize) return 'no-bridge'; "
            f"window.vemuSetResize({json.dumps(setting)}); return 'ok'; "
            "})()"
        )

    def grab_keyboard(self, grabbed: bool) -> None:
        self._rfb_action("rfb.focus();" if grabbed else "rfb.blur();")

    def reconnect(self) -> None:
        self._disconnected_since = None
        self._reconnect_stopped = False
        self._run_js(
            "(() => { const ui = window.vemuUI; "
            "if (!ui) return 'no-ui'; "
            "if (window.vemuReconnect) { window.vemuReconnect(); } "
            "else { ui.reconnect(); } return 'ok'; })()"
        )

    def _poll_status(self) -> None:
        self._run_js(
            "(() => { const ui = window.vemuUI; "
            "if (!ui) return 'init'; "
            "return ui.connected ? 'connected' : 'disconnected'; })()",
            self._set_status,
        )
        self._run_js(
            "(() => { const ui = window.vemuUI; "
            "const rfb = ui && ui.rfb; "
            "if (!rfb || !rfb._fbWidth || !rfb._fbHeight) return null; "
            "return JSON.stringify([rfb._fbWidth, rfb._fbHeight]); })()",
            self._set_frame_size,
        )

    def _set_frame_size(self, size: object) -> None:
        if isinstance(size, str):
            try:
                size = json.loads(size)
            except json.JSONDecodeError:
                return
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            return
        try:
            width, height = int(size[0]), int(size[1])
        except (TypeError, ValueError):
            return
        if width <= 0 or height <= 0:
            return
        if (width, height) != self._last_frame:
            self._last_frame = (width, height)
            self.frame_size_changed.emit(width, height)

    def _set_status(self, status: object) -> None:
        value = str(status or "init")
        now = time.monotonic()
        if value == "connected":
            self._disconnected_since = None
            self._reconnect_stopped = False
        elif value == "disconnected":
            if self._disconnected_since is None:
                self._disconnected_since = now
            elif (
                not self._reconnect_stopped
                and now - self._disconnected_since >= 60
            ):
                self._reconnect_stopped = True
                self._run_js(
                    "(() => { if (window.vemuCancelReconnect) "
                    "{ window.vemuCancelReconnect(); } return 'ok'; })()"
                )
                value = "timeout"
        if value != self._last_status:
            self._last_status = value
            self.connection_changed.emit(value)

    def capture_screenshot(
        self,
        callback: Callable[[Path | None, str | None], None] | None = None,
    ) -> None:
        script = """
        (() => {
          const canvas = document.querySelector('#noVNC_container canvas');
          if (!canvas || !canvas.width || !canvas.height) return null;
          return canvas.toDataURL('image/jpeg', 0.92);
        })()
        """

        def done(data_url) -> None:
            try:
                if not isinstance(data_url, str) or not data_url:
                    raise ValueError("console canvas is not ready")
                path = save_console_screenshot(data_url, self.domain_name)
            except Exception as exc:  # noqa: BLE001 - report save failure
                if callback is not None:
                    callback(None, str(exc))
                return
            if callback is not None:
                callback(path, None)

        self.view.page().runJavaScript(script, done)

    def stop(self) -> None:
        self._status_timer.stop()
        self._clipboard_timer.stop()
        self.server.stop()


def find_serial_dir() -> Path:
    directory = Path(__file__).parent / "web" / "terminal"
    if not (directory / "serial.html").is_file():
        raise FileNotFoundError("Serial console assets were not found.")
    return directory


class _SerialBridge(QObject):
    dataReceived = Signal(str)

    def __init__(self, write, resize, parent=None) -> None:
        super().__init__(parent)
        self._write = write
        self._resize = resize
        self._ready = False
        self._pending: list[str] = []

    def push(self, text: str) -> None:
        if not text:
            return
        if self._ready:
            self.dataReceived.emit(text)
        else:
            self._pending.append(text)

    @Slot(str)
    def send(self, data: str) -> None:
        self._write(data)

    @Slot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        self._resize(cols, rows)

    @Slot(str)
    def copy(self, text: str) -> None:
        QApplication.clipboard().setText(text)

    @Slot(result=str)
    def paste(self) -> str:
        return QApplication.clipboard().text()

    @Slot()
    def ready(self) -> None:
        self._ready = True
        pending, self._pending = self._pending, []
        for text in pending:
            self.dataReceived.emit(text)


class SerialTab(QWidget):
    """VT100/xterm.js serial console connected to a QEMU pty."""

    def __init__(self, info: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.domain_name = str(info.get("domain", "VM"))
        self.info = info
        self._uri = str(info.get("uri", "qemu:///system"))
        self._master: int | None = None
        self._process: subprocess.Popen | None = None
        self._notifier: QSocketNotifier | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stopped = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.server = ConsoleServer(find_serial_dir())
        port = self.server.start()
        self.view = QWebEngineView(self)
        layout.addWidget(self.view)
        settings = self.view.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard,
            True,
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptCanPaste, True
        )
        self.bridge = _SerialBridge(self._write, self._resize, self)
        self.channel = QWebChannel(self)
        self.channel.registerObject("serial", self.bridge)
        self.view.page().setWebChannel(self.channel)
        self.view.setUrl(
            QUrl(f"http://127.0.0.1:{port}/serial.html")
        )
        self._start_process()

    def _start_process(self) -> None:
        virsh = shutil.which("virsh")
        if not virsh:
            self.bridge.push(
                "Serial console unavailable: virsh was not found.\r\n"
            )
            return
        try:
            master, slave = pty.openpty()
            fcntl.ioctl(
                slave,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 40, 120, 0, 0),
            )
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            self._process = subprocess.Popen(
                [virsh, "-c", self._uri, "console", self.domain_name],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                env=env,
                preexec_fn=os.setsid,
            )
            os.close(slave)
            self._master = master
            self._notifier = QSocketNotifier(
                master, QSocketNotifier.Type.Read, self
            )
            self._notifier.activated.connect(self._read)
        except Exception as exc:  # noqa: BLE001 - show in terminal
            self.bridge.push(
                f"Serial console unavailable: {exc}\r\n"
            )

    def _read(self) -> None:
        if self._master is None:
            return
        try:
            data = os.read(self._master, 65536)
        except OSError:
            data = b""
        if not data:
            self._notifier_off()
            return
        text = self._decoder.decode(data)
        if text:
            self.bridge.push(text)

    def _write(self, data: str) -> None:
        if self._master is None or not data:
            return
        try:
            os.write(self._master, data.encode("utf-8"))
        except OSError:
            self._notifier_off()

    def _resize(self, cols: int, rows: int) -> None:
        if self._master is None:
            return
        try:
            fcntl.ioctl(
                self._master,
                termios.TIOCSWINSZ,
                struct.pack(
                    "HHHH",
                    max(1, min(rows, 65535)),
                    max(1, min(cols, 65535)),
                    0,
                    0,
                ),
            )
        except OSError:
            pass

    def _notifier_off(self) -> None:
        if self._notifier is not None:
            self._notifier.setEnabled(False)

    def stop(self) -> None:
        self._stopped = True
        self._notifier_off()
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
            self._process = None
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None
        self.server.stop()


class _ClipboardWorker(QThread):
    """Run one blocking guest-agent clipboard call off the UI thread."""

    done = Signal(object, object)

    def __init__(self, operation, parent=None) -> None:
        super().__init__(parent)
        self._operation = operation

    def run(self) -> None:
        try:
            result = self._operation()
        except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
            self.done.emit(None, exc)
        else:
            self.done.emit(result, None)


class ConsoleWindow(QMainWindow):
    """Standalone console window for a single VM."""

    closed = Signal(str)

    def __init__(
        self,
        info: dict[str, Any] | None = None,
        parent=None,
        backend=None,
        tokens=None,
    ) -> None:
        super().__init__(parent)
        self.info = dict(info or {})
        self.name = str(self.info.get("domain", "VM"))
        self.backend = backend
        self.tokens = tokens or (
            DARK if bool(getattr(parent, "dark", True)) else LIGHT
        )
        self.tab: ConsoleTab | None = None
        self.serial_tab: SerialTab | None = None
        self.mode = "graphics"
        self._closed = False
        self._icon_buttons: list[tuple[QPushButton | QToolButton, str]] = []
        self._shortcuts: dict[str, QShortcut] = {}
        self._scale_mode = "fit"
        self._saved_size = None
        self._pre_toolbar_size = None
        self._frame_size = (0, 0)
        self._settings = QSettings("v-EMU", "v-EMU")
        self._clipboard_workers: set[_ClipboardWorker] = set()
        self.low_latency = bool(
            self._settings.value(
                "ui/console_low_latency", False, type=bool
            )
        )
        self.clipboard_sync = bool(
            self._settings.value(
                "ui/console_clipboard_sync", True, type=bool
            )
        )
        self.pointer_dot = bool(
            self._settings.value(
                "ui/console_pointer_dot", False, type=bool
            )
        )
        self._last_guest_clipboard = ""
        self._last_sent_clipboard = ""
        self._pending_host_clipboard = ""
        self._suppress_host_clipboard = False
        self._clipboard_agent_ready = False
        self._clipboard_agent_starting = False
        self._connection_status = "init"
        self.setWindowTitle(f"{self.name} on QEMU/KVM")
        self.resize(1100, 720)

        self.stack = QStackedWidget(self)
        if self.info:
            self.tab = ConsoleTab(
                self.info,
                self,
                low_latency=self.low_latency,
                pointer_dot=self.pointer_dot,
            )
            self.stack.addWidget(self.tab)
            self.tab.connection_changed.connect(self._connection_changed)
            self.tab.frame_size_changed.connect(self._frame_size_changed)
            self.tab.clipboard_received.connect(self._guest_clipboard)
        self.stack.addWidget(QWidget())
        self.setCentralWidget(self.stack)

        self._build_toolbar()
        self._build_shortcuts()
        # The native toolbar starts hidden so the guest fills the window.
        # F9 or Ctrl+Shift+T reveals it; the hotkeys below cover the common
        # toolbar actions without showing it.
        self.toolbar.setVisible(False)
        self.toolbar_action.setChecked(False)
        self._refresh_power_state()

        QApplication.clipboard().dataChanged.connect(
            self._host_clipboard_changed
        )
        self._clipboard_sync_timer = QTimer(self)
        self._clipboard_sync_timer.setInterval(1200)
        self._clipboard_sync_timer.timeout.connect(
            self._poll_guest_clipboard
        )
        # A Windows guest may still be at the login screen when the console
        # opens. Retry the user-session agent so auto-sync starts after login
        # instead of staying disabled until the console is reopened.
        self._clipboard_retry_timer = QTimer(self)
        self._clipboard_retry_timer.setInterval(5000)
        self._clipboard_retry_timer.timeout.connect(
            self._retry_clipboard_agent
        )
        if self.clipboard_sync:
            self._clipboard_retry_timer.start()
        QTimer.singleShot(0, self._ensure_clipboard_agent)

    # ------------------------------------------------------------------ UI

    def _icon(self, name: str):
        return make_icon(name, self.tokens.muted, self.tokens.text)

    def _icon_button(
        self, name: str, tooltip: str, callback
    ) -> QPushButton:
        button = QPushButton(self)
        button.setObjectName("InspectorAction")
        button.setFixedSize(30, 30)
        button.setIconSize(QSize(18, 18))
        button.setIcon(self._icon(name))
        attach_tooltip(button, tooltip)
        button.clicked.connect(callback)
        self._icon_buttons.append((button, name))
        return button

    def _menu_button(self, text: str, tooltip: str, menu: QMenu) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setMenu(menu)
        button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        return button

    def _build_toolbar(self) -> None:
        bar = QToolBar("Console", self)
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setObjectName("ConsoleToolbar")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self.toolbar = bar

        bar.addWidget(
            self._menu_button(
                "Power",
                "Start or force off; use VM actions for ACPI shutdown/reboot",
                self._power_menu(),
            )
        )
        bar.addWidget(
            self._menu_button("Send Key", "Send key combinations", self._send_key_menu())
        )
        bar.addWidget(
            self._menu_button("View", "Display and toolbar options", self._view_menu())
        )
        bar.addWidget(
            self._menu_button("Mode", "Graphics or serial console", self._mode_menu())
        )
        bar.addSeparator()

        self.grab_button = QToolButton(self)
        self.grab_button.setText("Grab")
        attach_tooltip(self.grab_button, "Grab/release keyboard input")
        self.grab_button.setCheckable(True)
        self.grab_button.toggled.connect(self._grab_keyboard)
        bar.addWidget(self.grab_button)

        self.reconnect_button = QToolButton(self)
        self.reconnect_button.setText("Reconnect")
        attach_tooltip(self.reconnect_button, "Reconnect the console")
        self.reconnect_button.clicked.connect(self._reconnect)
        bar.addWidget(self.reconnect_button)

        self.screenshot_button = self._icon_button(
            "snapshot",
            "Save screenshot (Ctrl+Alt+Print)",
            self._capture_screenshot,
        )
        bar.addWidget(self.screenshot_button)
        self.fullscreen_button = self._icon_button(
            "fullscreen",
            "Toggle fullscreen (Ctrl+Alt+F)",
            self._toggle_fullscreen,
        )
        bar.addWidget(self.fullscreen_button)
        self.hide_toolbar_button = self._icon_button(
            "menu", "Hide toolbar (F9)", lambda: self._toggle_toolbar()
        )
        bar.addWidget(self.hide_toolbar_button)

    def _power_menu(self) -> QMenu:
        # ACPI shutdown/reboot can hang while firmware is still booting or
        # before the guest agent is available. Keep only safe console actions;
        # use the VM context menu for ACPI shutdown/reboot.
        menu = QMenu(self)
        self.start_action = menu.addAction("Start")
        self.start_action.triggered.connect(lambda: self._power("start"))
        menu.addSeparator()
        self.force_action = menu.addAction("Force off")
        self.force_action.triggered.connect(lambda: self._power("force"))
        return menu

    def _send_key_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("Ctrl+Alt+Del", self._send_ctrl_alt_del)
        menu.addAction(
            "Ctrl+Alt+Backspace",
            lambda: self._send_keys(
                [
                    (KEY_CTRL, "ControlLeft"),
                    (KEY_ALT, "AltLeft"),
                    (KEY_BACKSPACE, "Backspace"),
                ]
            ),
        )
        menu.addAction(
            "PrintScreen",
            lambda: self._send_keys([(KEY_PRINT, "PrintScreen")]),
        )
        menu.addSeparator()
        for number in range(1, 13):
            menu.addAction(
                f"Ctrl+Alt+F{number}",
                lambda n=number: self._send_keys(
                    [
                        (KEY_CTRL, "ControlLeft"),
                        (KEY_ALT, "AltLeft"),
                        (KEY_F[n], f"F{n}"),
                    ]
                ),
            )
        return menu

    def _view_menu(self) -> QMenu:
        menu = QMenu(self)
        self.fit_action = menu.addAction("Fit window")
        self.fit_action.setCheckable(True)
        self.fit_action.setChecked(True)
        self.fit_action.triggered.connect(lambda: self._set_scale("fit"))
        self.actual_action = menu.addAction("Actual size")
        self.actual_action.setCheckable(True)
        self.actual_action.triggered.connect(
            lambda: self._set_scale("actual")
        )
        menu.addSeparator()
        self.low_latency_action = menu.addAction(
            "Low latency (reduce quality)"
        )
        self.low_latency_action.setCheckable(True)
        self.low_latency_action.setChecked(self.low_latency)
        self.low_latency_action.triggered.connect(self._set_low_latency)
        self.pointer_dot_action = menu.addAction("Show pointer dot")
        self.pointer_dot_action.setCheckable(True)
        self.pointer_dot_action.setChecked(self.pointer_dot)
        self.pointer_dot_action.setToolTip(
            "Show a local dot when the guest hides the cursor (e.g. some "
            "Windows pointer schemes)."
        )
        self.pointer_dot_action.triggered.connect(self._set_pointer_dot)
        self.clipboard_sync_action = menu.addAction(
            "Clipboard sync (guest ↔ host)"
        )
        self.clipboard_sync_action.setCheckable(True)
        self.clipboard_sync_action.setChecked(self.clipboard_sync)
        self.clipboard_sync_action.triggered.connect(
            self._set_clipboard_sync
        )
        # Toolbar/fullscreen have their own buttons; still keep a checkable
        # action for the F9 state.
        self.toolbar_action = QAction("Show toolbar", self)
        self.toolbar_action.setCheckable(True)
        self.toolbar_action.setChecked(True)
        return menu

    def _mode_menu(self) -> QMenu:
        menu = QMenu(self)
        self.graphics_mode_action = menu.addAction("Graphics")
        self.graphics_mode_action.setCheckable(True)
        self.graphics_mode_action.setChecked(True)
        self.graphics_mode_action.triggered.connect(
            lambda: self._set_mode("graphics")
        )
        self.serial_mode_action = menu.addAction("Serial")
        self.serial_mode_action.setCheckable(True)
        self.serial_mode_action.setEnabled(
            bool(self.info.get("serial"))
        )
        if not self.info.get("serial"):
            self.serial_mode_action.setToolTip(
                compact_tooltip(
                    "No serial device; add one in Configure hardware while "
                    "the VM is stopped."
                )
            )
        self.serial_mode_action.triggered.connect(
            lambda: self._set_mode("serial")
        )
        return menu

    def _set_mode(self, mode: str) -> None:
        if mode == "serial":
            if not self.info.get("serial"):
                return
            if self.serial_tab is None:
                try:
                    self.serial_tab = SerialTab(self.info, self)
                except Exception as exc:  # noqa: BLE001 - show a clear error
                    QMessageBox.critical(
                        self, "Serial console", str(exc)
                    )
                    return
                self.stack.addWidget(self.serial_tab)
            self.stack.setCurrentWidget(self.serial_tab)
            self.mode = "serial"
        else:
            if self.tab is not None:
                self.stack.setCurrentWidget(self.tab)
            self.mode = "graphics"
        self.graphics_mode_action.setChecked(mode == "graphics")
        self.serial_mode_action.setChecked(mode == "serial")

    def _build_shortcuts(self) -> None:
        # Window-level shortcuts so they still work while the noVNC web view
        # holds keyboard focus. Ctrl+Alt+Insert sends Ctrl+Alt+Del instead of
        # the literal Insert chord.
        bindings = (
            ("toolbar", "F9", self._toggle_toolbar),
            ("toolbar-alt", "Ctrl+Shift+T", self._toggle_toolbar),
            ("ctrl-alt-del", "Ctrl+Alt+Insert", self._send_ctrl_alt_del),
            ("fullscreen", "Ctrl+Alt+F", self._toggle_fullscreen),
            ("screenshot", "Ctrl+Alt+Print", self._capture_screenshot),
            ("paste", "Ctrl+Alt+V", self._paste),
        )
        for name, sequence, callback in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts[name] = shortcut
        self._escape_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._escape_shortcut.activated.connect(self._exit_fullscreen)
        self._escape_shortcut.setEnabled(False)

    # --------------------------------------------------------------- actions

    def _send_ctrl_alt_del(self) -> None:
        if self.tab is not None:
            self.tab.send_ctrl_alt_del()

    def _send_keys(self, keys: list[tuple[int, str]]) -> None:
        if self.tab is not None:
            self.tab.send_key_chord(keys)

    def _paste(self) -> None:
        if self.tab is None:
            return
        text = QApplication.clipboard().text()
        if not text:
            notify("v-EMU", "Host clipboard is empty")
            return
        if self.backend is not None and hasattr(
            self.backend, "vm_clipboard_set"
        ):
            notify("v-EMU", "Sending clipboard to the guest…")
            self._start_clipboard_task(
                lambda: self.backend.vm_clipboard_set(self.name, text),
                lambda result, error: self._paste_finished(
                    text, result, error
                ),
            )
            return
        self.tab.paste_text(text)

    def _paste_finished(self, text: str, _result, error) -> None:
        if error is None:
            self._clipboard_agent_ready = True
            self._clipboard_retry_timer.stop()
            self._pending_host_clipboard = ""
            if self.clipboard_sync:
                self._clipboard_sync_timer.start()
                self._poll_guest_clipboard()
            notify("v-EMU", "Clipboard sent to the guest")
            return
        # QEMU's VNC clipboard has no guest-OS peer, but keep the best-effort
        # RFB send for guests that run their own VNC clipboard helper. Never
        # report it as a successful guest paste.
        if self.tab is not None:
            self.tab.paste_text(text)
        message = str(error)
        if self._session_error(message):
            # The user logged out or the session is still starting. Retry the
            # helper and deliver this text after the next login.
            self._pending_host_clipboard = text
            self._clipboard_agent_ready = False
            if self.clipboard_sync:
                self._clipboard_retry_timer.start()
        notify(
            "Clipboard not sent to the guest",
            self._clipboard_error(error),
        )

    def _copy_guest_clipboard(self) -> None:
        if self.backend is None or not hasattr(
            self.backend, "vm_clipboard_get"
        ):
            notify("v-EMU", "Guest clipboard requires the v-EMU daemon")
            return
        notify("v-EMU", "Reading the guest clipboard…")
        self._start_clipboard_task(
            lambda: self.backend.vm_clipboard_get(self.name),
            self._copy_finished,
        )

    def _copy_finished(self, result, error) -> None:
        if error is not None:
            notify("v-EMU", self._clipboard_error(error))
            return
        text = ""
        if isinstance(result, dict):
            text = str(result.get("text", ""))
        if not text:
            notify("v-EMU", "Guest clipboard is empty")
            return
        QApplication.clipboard().setText(text)
        notify("v-EMU", "Copied guest clipboard")

    @staticmethod
    def _clipboard_error(error) -> str:
        message = str(error)
        if "Unknown method: vm.clipboard" in message:
            return (
                "vemud is out of date; run ./setup.sh --standard "
                "and restart v-EMU"
            )
        if "No active Windows user session" in message:
            return (
                "Log in to the Windows guest, then try again. "
                "Clipboard sync needs an active user session."
            )
        if ConsoleWindow._session_error(message):
            return (
                "Log in to a Wayland desktop session in the Linux guest, "
                "then try again."
            )
        if ConsoleWindow._clipboard_config_error(message):
            if "wl-clipboard" in message and "guest-exec" not in message:
                return "Install wl-clipboard in the guest, then try again."
            return (
                "The guest blocks the QEMU agent guest-exec RPC. Allow "
                "guest-exec in the guest's qemu-ga policy, install "
                "wl-clipboard, then restart qemu-guest-agent."
            )
        return f"Guest clipboard unavailable: {message}"

    @staticmethod
    def _session_error(message: str) -> bool:
        return (
            "No active Windows user session" in message
            or "No active Linux user session" in message
            or "No active guest user session" in message
            or "requires an active Wayland session" in message
        )

    @staticmethod
    def _clipboard_config_error(message: str) -> bool:
        return "wl-clipboard" in message or (
            "guest-exec" in message
            and (
                "blocks" in message
                or "disabled" in message
                or "not allowed" in message
            )
        )

    def _retry_clipboard_agent(self) -> None:
        if (
            self._closed
            or not self.clipboard_sync
            or self._connection_status != "connected"
        ):
            return
        self._ensure_clipboard_agent()

    def _ensure_clipboard_agent(self) -> None:
        if (
            self._closed
            or self.backend is None
            or self._clipboard_agent_ready
            or self._clipboard_agent_starting
        ):
            return
        if not hasattr(self.backend, "vm_clipboard_agent_start"):
            self._clipboard_retry_timer.stop()
            return
        self._clipboard_agent_starting = True

        def finished(_result, error) -> None:
            self._clipboard_agent_starting = False
            if error is not None:
                message = str(error)
                if (
                    "Unknown method" in message
                    or "out of date" in message
                    or self._clipboard_config_error(message)
                ):
                    self._clipboard_retry_timer.stop()
                return
            self._clipboard_agent_ready = True
            self._clipboard_retry_timer.stop()
            if self.clipboard_sync:
                self._clipboard_sync_timer.start()
                if self._pending_host_clipboard:
                    # A host copy that failed while the guest was at the
                    # sign-in screen is delivered once the agent is ready.
                    text = self._pending_host_clipboard
                    self._pending_host_clipboard = ""
                    self._last_sent_clipboard = text
                    self._start_clipboard_task(
                        lambda: self.backend.vm_clipboard_set(
                            self.name, text
                        ),
                        lambda result, error: self._host_clipboard_sent(
                            text, result, error
                        ),
                    )
                else:
                    self._poll_guest_clipboard()

        self._start_clipboard_task(
            lambda: self.backend.vm_clipboard_agent_start(self.name),
            finished,
        )

    def _start_clipboard_task(self, operation, callback) -> None:
        worker = _ClipboardWorker(operation, self)
        self._clipboard_workers.add(worker)

        def finished(result, error) -> None:
            self._clipboard_workers.discard(worker)
            worker.deleteLater()
            if not self._closed:
                callback(result, error)

        worker.done.connect(finished)
        worker.start()

    def _set_clipboard_sync(self, enabled: bool) -> None:
        self.clipboard_sync = bool(enabled)
        self._settings.setValue(
            "ui/console_clipboard_sync", self.clipboard_sync
        )
        if self.clipboard_sync:
            self._clipboard_retry_timer.start()
            self._ensure_clipboard_agent()
            if self._clipboard_agent_ready:
                self._clipboard_sync_timer.start()
                self._poll_guest_clipboard()
        else:
            self._clipboard_sync_timer.stop()
            self._clipboard_retry_timer.stop()
            self._pending_host_clipboard = ""

    def _poll_guest_clipboard(self) -> None:
        if (
            not self.clipboard_sync
            or not self._clipboard_agent_ready
            or self._closed
            or self.backend is None
            or not hasattr(self.backend, "vm_clipboard_get")
            or self._clipboard_workers
        ):
            return
        self._start_clipboard_task(
            lambda: self.backend.vm_clipboard_get(self.name),
            self._poll_guest_finished,
        )

    def _poll_guest_finished(self, result, error) -> None:
        if error is not None:
            message = str(error)
            if "Unknown method" in message:
                self._clipboard_sync_timer.stop()
            elif self._clipboard_config_error(message):
                self._clipboard_agent_ready = False
                self._clipboard_sync_timer.stop()
                self._clipboard_retry_timer.stop()
            elif self._session_error(message):
                self._clipboard_agent_ready = False
                self._clipboard_sync_timer.stop()
                if self.clipboard_sync:
                    self._clipboard_retry_timer.start()
            return
        text = ""
        if isinstance(result, dict):
            text = str(result.get("text", ""))
        if not text or text == self._last_guest_clipboard:
            return
        self._last_guest_clipboard = text
        if text == QApplication.clipboard().text():
            return
        self._suppress_host_clipboard = True
        QApplication.clipboard().setText(text)
        QTimer.singleShot(
            100, lambda: setattr(self, "_suppress_host_clipboard", False)
        )

    def _host_clipboard_changed(self) -> None:
        if (
            not self.clipboard_sync
            or self._suppress_host_clipboard
            or not self._clipboard_agent_ready
            or self._closed
            or self.backend is None
            or not hasattr(self.backend, "vm_clipboard_set")
            or not self.isActiveWindow()
        ):
            return
        text = QApplication.clipboard().text()
        if (
            not text
            or text == self._last_guest_clipboard
            or text == self._last_sent_clipboard
        ):
            return
        self._last_sent_clipboard = text
        self._start_clipboard_task(
            lambda: self.backend.vm_clipboard_set(self.name, text),
            lambda result, error: self._host_clipboard_sent(
                text, result, error
            ),
        )

    def _host_clipboard_sent(self, text: str, _result, error) -> None:
        if error is None:
            # Any successful send supersedes older pending clipboard text.
            self._pending_host_clipboard = ""
            return
        # Allow the same text to be retried after the guest session returns.
        if self._last_sent_clipboard == text:
            self._last_sent_clipboard = ""
        message = str(error)
        if self._session_error(message):
            self._pending_host_clipboard = text
            self._clipboard_agent_ready = False
            if self.clipboard_sync:
                self._clipboard_retry_timer.start()

    def _guest_clipboard(self, text: str) -> None:
        if not text:
            return
        clipboard = QApplication.clipboard()
        if clipboard.text() != text:
            clipboard.setText(text)

    def _set_low_latency(self, enabled: bool) -> None:
        self.low_latency = bool(enabled)
        self._settings.setValue(
            "ui/console_low_latency", self.low_latency
        )
        if self.tab is not None:
            self.tab.set_low_latency(self.low_latency)

    def _set_pointer_dot(self, enabled: bool) -> None:
        self.pointer_dot = bool(enabled)
        self.pointer_dot_action.setChecked(self.pointer_dot)
        self._settings.setValue(
            "ui/console_pointer_dot", self.pointer_dot
        )
        if self.tab is not None:
            self.tab.set_pointer_dot(self.pointer_dot)

    def _grab_keyboard(self, grabbed: bool) -> None:
        if self.tab is not None:
            self.tab.grab_keyboard(grabbed)

    def _set_scale(self, mode: str) -> None:
        previous = self._scale_mode
        self._scale_mode = mode
        if self.tab is not None:
            self.tab.set_scale(mode)
        for action, value in (
            (self.fit_action, "fit"),
            (self.actual_action, "actual"),
        ):
            action.setChecked(value == mode)
        if mode == "actual":
            if previous != "actual":
                if not self.isFullScreen():
                    self._saved_size = self.size()
                self._resize_to_frame()
        else:
            if previous == "actual" and self._saved_size:
                if not self.isFullScreen():
                    self.resize(self._saved_size)
                self._saved_size = None
            if not self.isFullScreen() and not self.toolbar.isVisible():
                self._resize_to_fit_aspect()

    def _frame_size_changed(self, width: int, height: int) -> None:
        self._frame_size = (width, height)
        if self._scale_mode == "actual":
            self._resize_to_frame()
        elif not self.toolbar.isVisible():
            self._resize_to_fit_aspect()

    def _resize_to_frame(self) -> None:
        if self.isFullScreen():
            return
        width, height = self._frame_size
        if width <= 0 or height <= 0:
            return
        screen = self.screen()
        available = screen.availableGeometry() if screen is not None else None
        chrome = (
            self.toolbar.sizeHint().height()
            if self.toolbar.isVisible()
            else 0
        )
        target_w = width
        target_h = height + chrome
        if available is not None:
            target_w = min(target_w, available.width())
            target_h = min(target_h, available.height())
        self.resize(target_w, target_h)
        self._keep_on_screen()

    def _resize_to_fit_aspect(self) -> None:
        """Make the client area match the guest aspect ratio.

        Used when the toolbar is hidden, so no letterbox bars remain above or
        below the guest.
        """
        if self.isFullScreen():
            return
        width, height = self._frame_size
        if width <= 0 or height <= 0:
            return
        screen = self.screen()
        available = screen.availableGeometry() if screen is not None else None
        target_w = self.width()
        if available is not None:
            target_w = min(target_w, available.width())
        target_h = round(target_w * height / width)
        if available is not None and target_h > available.height():
            target_h = available.height()
            target_w = round(target_h * width / height)
        self.resize(target_w, target_h)
        self._keep_on_screen()

    def _keep_on_screen(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        right = self.x() + self.width()
        bottom = self.y() + self.height()
        x = self.x()
        y = self.y()
        if right > available.right():
            x = max(available.left(), available.right() - self.width())
        if bottom > available.bottom():
            y = max(available.top(), available.bottom() - self.height())
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _reconnect(self) -> None:
        if self.tab is not None:
            self.tab.reconnect()

    def _capture_screenshot(self) -> None:
        if self.tab is not None:
            self.tab.capture_screenshot(self._screenshot_done)

    def _screenshot_done(self, path, error) -> None:
        if error:
            QMessageBox.warning(self, "Screenshot failed", error)
        elif path is not None:
            notify("Screenshot saved", str(path))

    def _connection_changed(self, status: str) -> None:
        self._connection_status = status
        if status == "connected":
            self._clipboard_agent_ready = False
            if self.clipboard_sync:
                self._clipboard_retry_timer.start()
            self._ensure_clipboard_agent()
        else:
            self._clipboard_retry_timer.stop()
        label = {
            "connected": "Connected",
            "disconnected": "Disconnected",
            "timeout": "Disconnected",
            "init": "Connecting…",
        }.get(status, status)
        self.setWindowTitle(f"{self.name} on QEMU/KVM — {label}")

    # ----------------------------------------------------------------- power

    def _power(self, action: str) -> None:
        if self.backend is None:
            return
        if action == "force":
            answer = QMessageBox.question(
                self,
                "Force off",
                f"Force off {self.name}? Unsaved guest data will be lost.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            if action == "start":
                self.backend.start(self.name)
            else:
                self.backend.destroy(self.name)
        except Exception as exc:  # noqa: BLE001 - report backend errors
            QMessageBox.critical(self, "Power action failed", str(exc))
            return
        notify("v-EMU", f"{action.capitalize()} requested for {self.name}")
        QTimer.singleShot(1500, self._refresh_power_state)

    def _refresh_power_state(self) -> None:
        if self.backend is None or not hasattr(self.backend, "list_domains"):
            for action in (self.start_action, self.force_action):
                action.setEnabled(False)
            return
        try:
            domains = self.backend.list_domains()
        except Exception:  # noqa: BLE001 - keep previous menu state
            return
        state = next(
            (item.state for item in domains if item.name == self.name),
            "stopped",
        )
        running = state == "running"
        self.start_action.setEnabled(not running)
        self.force_action.setEnabled(running)

    # ------------------------------------------------------------- fullscreen

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self._exit_fullscreen()
            return
        # Fullscreen should always fill the screen, independent of the
        # windowed fit/actual mode. The user can still choose Actual size
        # from the View menu after entering fullscreen.
        self._set_scale("fit")
        self.showFullScreen()
        self._escape_shortcut.setEnabled(True)
        self._sync_fullscreen_icon()

    def _exit_fullscreen(self) -> None:
        if not self.isFullScreen():
            return
        self.showNormal()
        self._set_scale("fit")
        self._escape_shortcut.setEnabled(False)
        self._sync_fullscreen_icon()

    def _toggle_toolbar(self, visible=None) -> None:
        if visible is None:
            visible = not self.toolbar_action.isChecked()
        visible = bool(visible)
        if not visible:
            if not self.isFullScreen() and self._pre_toolbar_size is None:
                self._pre_toolbar_size = self.size()
            self.toolbar.setVisible(False)
            self.toolbar_action.setChecked(False)
            if self._scale_mode == "actual":
                self._resize_to_frame()
            elif not self.isFullScreen():
                self._resize_to_fit_aspect()
        else:
            self.toolbar.setVisible(True)
            self.toolbar_action.setChecked(True)
            if self._pre_toolbar_size is not None:
                self.resize(self._pre_toolbar_size)
                self._pre_toolbar_size = None
            elif self._scale_mode == "actual":
                self._resize_to_frame()

    def _sync_fullscreen_icon(self) -> None:
        name = "restore" if self.isFullScreen() else "fullscreen"
        self.fullscreen_button.setIcon(self._icon(name))
        for index, (button, icon_name) in enumerate(self._icon_buttons):
            if button is self.fullscreen_button:
                self._icon_buttons[index] = (button, name)
                break

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_fullscreen_icon()
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                QTimer.singleShot(0, self._host_clipboard_changed)
            elif self.grab_button.isChecked():
                self.grab_button.setChecked(False)

    def set_theme(self, tokens) -> None:
        self.tokens = tokens
        for button, name in self._icon_buttons:
            button.setIcon(self._icon(name))
        self._sync_fullscreen_icon()

    def shutdown(self) -> None:
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._clipboard_sync_timer.stop()
        self._clipboard_retry_timer.stop()
        for worker in list(self._clipboard_workers):
            worker.wait(5000)
        if self.serial_tab is not None:
            self.serial_tab.stop()
            self.serial_tab.deleteLater()
            self.serial_tab = None
        if self.tab is not None:
            self.tab.stop()
            self.tab.deleteLater()
            self.tab = None
        if not self._closed:
            self._closed = True
            self.closed.emit(self.name)
        super().closeEvent(event)
