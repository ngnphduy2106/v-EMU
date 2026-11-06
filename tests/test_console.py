from __future__ import annotations

import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

from vemu.backend import ensure_vnc_websocket, parse_vnc_graphics
from vemu.errors import Conflict, NotFound
from vemu.ui.console import (
    ConsoleServer,
    console_acceleration_enabled,
    console_url,
    find_novnc_dir,
    find_serial_dir,
)

VNC_XML = """<domain type='kvm'>
  <devices>
    <graphics type='vnc' port='5900' autoport='yes' websocket='5700' listen='127.0.0.1'/>
  </devices>
</domain>"""


class BackendConsoleTests(unittest.TestCase):
    def test_parse_vnc(self) -> None:
        info = parse_vnc_graphics(VNC_XML)
        self.assertEqual(info["host"], "127.0.0.1")
        self.assertEqual(info["port"], 5900)
        self.assertEqual(info["websocket"], 5700)
    def test_parse_missing(self) -> None:
        with self.assertRaises(NotFound):
            parse_vnc_graphics("<domain><devices/></domain>")

    def test_parse_unallocated(self) -> None:
        xml = VNC_XML.replace("5900", "-1").replace("5700", "-1")
        with self.assertRaises(Conflict):
            parse_vnc_graphics(xml)

    def test_ensure_vnc_websocket(self) -> None:
        xml = """<domain><devices>
          <graphics type='vnc' port='5900' autoport='yes'/>
        </devices></domain>"""
        updated = ensure_vnc_websocket(xml)
        self.assertIn('websocket="-1"', updated)
        self.assertEqual(ensure_vnc_websocket(updated), updated)
        no_vnc = "<domain><devices/></domain>"
        self.assertEqual(ensure_vnc_websocket(no_vnc), no_vnc)

    def test_bundled_novnc(self) -> None:
        directory = find_novnc_dir()
        self.assertTrue((directory / "vnc.html").is_file())

    def test_console_acceleration_follows_webengine_flag(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(console_acceleration_enabled())
        with mock.patch.dict(
            os.environ, {"VEMU_WEBENGINE_GPU": "1"}, clear=True
        ):
            self.assertTrue(console_acceleration_enabled())

    def test_console_url(self) -> None:
        url = console_url(
            {"host": "127.0.0.1", "websocket": 5700}, 1234
        ).toString()
        self.assertIn("http://127.0.0.1:1234/vnc.html?", url)
        self.assertIn("host=127.0.0.1", url)
        self.assertIn("port=5700", url)
        self.assertIn("path=&", url)
        self.assertIn("autoconnect=1", url)
        self.assertIn("bell=off", url)
        self.assertIn("quality=6", url)
        self.assertIn("compression=2", url)
        self.assertNotIn("view_only", url)

        low_latency = console_url(
            {"host": "127.0.0.1", "websocket": 5700},
            1234,
            quality=5,
            compression=0,
        ).toString()
        self.assertIn("quality=5", low_latency)
        self.assertIn("compression=0", low_latency)

        preview = console_url(
            {"host": "127.0.0.1", "websocket": 5700},
            1234,
            view_only=True,
        ).toString()
        self.assertIn("view_only=1", preview)

        pointer = console_url(
            {"host": "127.0.0.1", "websocket": 5700},
            1234,
            show_dot=True,
        ).toString()
        self.assertIn("show_dot=1", pointer)

    def test_serial_assets_and_bridge(self) -> None:
        from PySide6.QtWidgets import QApplication

        from vemu.ui.console import _SerialBridge

        directory = find_serial_dir()
        page = (directory / "serial.html").read_text(encoding="utf-8")
        self.assertIn("xterm.js", page)
        self.assertIn("serial-copy", page)
        self.assertIn("serial-paste", page)
        self.assertIn("KeyC", page)
        self.assertIn("KeyV", page)

        QApplication.instance() or QApplication([])
        QApplication.clipboard().setText("host text")
        writes: list[str] = []
        resizes: list[tuple[int, int]] = []
        bridge = _SerialBridge(
            writes.append, lambda cols, rows: resizes.append((cols, rows))
        )
        bridge.copy("guest text")
        self.assertEqual(QApplication.clipboard().text(), "guest text")
        self.assertEqual(bridge.paste(), "guest text")
        bridge.send("abc")
        bridge.resize(120, 40)
        self.assertEqual(writes, ["abc"])
        self.assertEqual(resizes, [(120, 40)])

    def test_domain_screenshot(self) -> None:
        import base64
        from types import SimpleNamespace
        from unittest import mock

        from vemu import service

        def fake_run(command, **_kwargs):
            Path(command[-1]).write_bytes(b"\x89PNG\r\n\x1a\nfake")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch(
            "vemu.service.shutil.which", return_value="/usr/bin/virsh"
        ), mock.patch("vemu.service.subprocess.run", side_effect=fake_run):
            payload = service.domain_screenshot("vm1")
        self.assertEqual(payload["format"], "png")
        self.assertEqual(
            base64.b64decode(payload["data"]),
            b"\x89PNG\r\n\x1a\nfake",
        )

    def test_console_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("hello console", encoding="utf-8")
            server = ConsoleServer(root)
            port = server.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/index.html", timeout=5
                ) as response:
                    self.assertEqual(response.read().decode(), "hello console")
            finally:
                server.stop()


class ConsoleWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_save_console_screenshot(self) -> None:
        import base64
        from vemu.ui.console import save_console_screenshot

        payload = b"\xff\xd8\xff\xe0fakejpeg"
        data_url = "data:image/jpeg;base64," + base64.b64encode(
            payload
        ).decode()
        with tempfile.TemporaryDirectory() as tmp:
            path = save_console_screenshot(
                data_url, "vm1", directory=Path(tmp)
            )
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), payload)
            self.assertTrue(path.name.endswith(".jpg"))

    def test_fullscreen_toggle(self) -> None:
        from vemu.ui.console import ConsoleWindow

        window = ConsoleWindow()
        try:
            self.assertTrue(window.fullscreen_button.isEnabled())
            window._toggle_fullscreen()
            self.assertTrue(window.isFullScreen())
            window._toggle_fullscreen()
            self.assertFalse(window.isFullScreen())
        finally:
            window.close()

    def test_console_window_has_hideable_toolbar(self) -> None:
        from vemu.ui.console import ConsoleWindow

        window = ConsoleWindow()
        try:
            self.assertFalse(hasattr(window, "tabs"))
            self.assertFalse(hasattr(window, "paste_button"))
            self.assertFalse(hasattr(window, "copy_button"))
            self.assertFalse(hasattr(window, "shutdown_action"))
            self.assertFalse(hasattr(window, "restart_action"))
            self.assertTrue(hasattr(window, "graphics_mode_action"))
            self.assertFalse(window.serial_mode_action.isEnabled())
            self.assertTrue(hasattr(window, "pointer_dot_action"))
            self.assertEqual(
                window.pointer_dot_action.isChecked(), window.pointer_dot
            )
            window._set_pointer_dot(True)
            self.assertTrue(window.pointer_dot)
            self.assertTrue(window.pointer_dot_action.isChecked())
            window._set_pointer_dot(False)
            self.assertFalse(window.pointer_dot)
            self.assertFalse(window.toolbar_action.isChecked())
            self.assertTrue(window.toolbar.isHidden())
            window._toggle_toolbar()
            self.assertTrue(window.toolbar_action.isChecked())
            self.assertFalse(window.toolbar.isHidden())
            window._toggle_toolbar()
            self.assertFalse(window.toolbar_action.isChecked())
            self.assertTrue(window.toolbar.isHidden())
        finally:
            window.close()

    def test_console_hotkeys(self) -> None:
        from PySide6.QtWidgets import QApplication

        from vemu.ui.console import ConsoleWindow

        class FakeTab:
            def __init__(self) -> None:
                self.calls: list[object] = []

            def send_ctrl_alt_del(self) -> None:
                self.calls.append("ctrl-alt-del")

            def capture_screenshot(self, callback=None) -> None:
                self.calls.append("screenshot")

            def paste_text(self, text: str) -> None:
                self.calls.append(("paste", text))

            def set_scale(self, mode: str) -> None:
                self.calls.append(("scale", mode))

        window = ConsoleWindow()
        try:
            self.assertEqual(
                {
                    name: shortcut.key().toString()
                    for name, shortcut in window._shortcuts.items()
                },
                {
                    "toolbar": "F9",
                    "toolbar-alt": "Ctrl+Shift+T",
                    "ctrl-alt-del": "Ctrl+Alt+Ins",
                    "fullscreen": "Ctrl+Alt+F",
                    "screenshot": "Ctrl+Alt+Print",
                    "paste": "Ctrl+Alt+V",
                },
            )
            fake = FakeTab()
            window.tab = fake
            QApplication.clipboard().setText("host clipboard")
            window._shortcuts["ctrl-alt-del"].activated.emit()
            window._shortcuts["screenshot"].activated.emit()
            window._shortcuts["paste"].activated.emit()
            self.assertEqual(
                fake.calls,
                [
                    "ctrl-alt-del",
                    "screenshot",
                    ("paste", "host clipboard"),
                ],
            )
            window._shortcuts["fullscreen"].activated.emit()
            self.assertTrue(window.isFullScreen())
            window._shortcuts["fullscreen"].activated.emit()
            self.assertFalse(window.isFullScreen())
        finally:
            window.tab = None
            window.close()

    def test_console_tab_accepts_pointer_dot(self) -> None:
        import inspect

        from vemu.ui.console import ConsoleTab

        parameters = inspect.signature(ConsoleTab.__init__).parameters
        self.assertIn("pointer_dot", parameters)

    def test_console_low_latency_and_clipboard_sync(self) -> None:
        from PySide6.QtWidgets import QApplication

        from vemu.ui.console import ConsoleWindow

        window = ConsoleWindow()
        try:
            self.assertTrue(hasattr(window, "low_latency_action"))
            self.assertEqual(
                window.low_latency_action.isChecked(),
                window.low_latency,
            )
            QApplication.clipboard().setText("before")
            window._guest_clipboard("from guest")
            self.assertEqual(
                QApplication.clipboard().text(), "from guest"
            )
            window._guest_clipboard("")
            self.assertEqual(
                QApplication.clipboard().text(), "from guest"
            )
            window._copy_finished({"text": "guest agent text"}, None)
            self.assertEqual(
                QApplication.clipboard().text(), "guest agent text"
            )
            window._copy_finished({"text": ""}, None)
            self.assertEqual(
                QApplication.clipboard().text(), "guest agent text"
            )
            self.assertIn(
                "setup.sh",
                window._clipboard_error(
                    RuntimeError("Unknown method: vm.clipboard_get")
                ),
            )
            self.assertIn(
                "Log in to the Windows guest",
                window._clipboard_error(
                    RuntimeError(
                        "No active Windows user session was found for "
                        "guest clipboard."
                    )
                ),
            )
            self.assertIn(
                "Wayland",
                window._clipboard_error(
                    RuntimeError(
                        "Linux guest clipboard requires an active "
                        "Wayland session."
                    )
                ),
            )
            self.assertIn(
                "guest-exec",
                window._clipboard_error(
                    RuntimeError(
                        "Linux guest clipboard needs the QEMU guest "
                        "agent guest-exec RPC, but the guest blocks it."
                    )
                ),
            )
            self.assertIn(
                "wl-clipboard",
                window._clipboard_error(
                    RuntimeError(
                        "Install wl-clipboard in the guest "
                        "(for example: sudo dnf install wl-clipboard)."
                    )
                ),
            )
        finally:
            window.close()

    def test_paste_finish_updates_clipboard_state(self) -> None:
        from vemu.ui.console import ConsoleWindow

        window = ConsoleWindow()
        try:
            window._clipboard_agent_ready = False
            window._clipboard_retry_timer.start()
            with mock.patch("vemu.ui.console.notify"):
                window._paste_finished("text", None, None)
            self.assertTrue(window._clipboard_agent_ready)
            self.assertFalse(window._clipboard_retry_timer.isActive())

            with mock.patch("vemu.ui.console.notify") as notify_mock:
                window._paste_finished(
                    "text",
                    None,
                    RuntimeError(
                        "No active Windows user session was found for "
                        "guest clipboard."
                    ),
                )
            self.assertEqual(
                notify_mock.call_args.args[0],
                "Clipboard not sent to the guest",
            )
            self.assertIn(
                "Log in to the Windows guest",
                notify_mock.call_args.args[1],
            )
            self.assertNotIn(
                "used VNC clipboard", notify_mock.call_args.args[1]
            )
            self.assertEqual(window._pending_host_clipboard, "text")
            window._clipboard_agent_ready = True
            window._clipboard_retry_timer.stop()
            window._poll_guest_finished(
                None,
                RuntimeError(
                    "No active Windows user session was found for "
                    "guest clipboard."
                ),
            )
            self.assertFalse(window._clipboard_agent_ready)
            self.assertTrue(window._clipboard_retry_timer.isActive())

            window._last_sent_clipboard = "pending text"
            window._host_clipboard_sent(
                "pending text",
                None,
                RuntimeError(
                    "No active Windows user session was found for "
                    "guest clipboard."
                ),
            )
            self.assertEqual(
                window._pending_host_clipboard, "pending text"
            )
            self.assertEqual(window._last_sent_clipboard, "")
            window._host_clipboard_sent("pending text", None, None)
            self.assertEqual(window._pending_host_clipboard, "")
        finally:
            window.close()

    def test_notification_uses_desktop_service(self) -> None:
        from vemu.ui import notify as notify_module

        with mock.patch(
            "vemu.ui.notify.shutil.which",
            side_effect=lambda name: (
                "/usr/bin/notify-send" if name == "notify-send" else None
            ),
        ), mock.patch("vemu.ui.notify.subprocess.Popen") as popen:
            notify_module.notify("Title", "Body")
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/notify-send")
        self.assertIn("Title", command)
        self.assertIn("Body", command)

    def test_mini_console_no_signal(self) -> None:
        from vemu.ui.console import MiniConsole

        class BadBackend:
            def screenshot(self, domain):
                raise RuntimeError("domain is not running")

        mini = MiniConsole(backend=BadBackend())
        try:
            mini.load({"domain": "vm1"})
            self.assertEqual(mini.preview.text(), "No signal")
        finally:
            mini.shutdown()

    def test_mini_console_empty(self) -> None:
        from vemu.ui.console import MiniConsole

        mini = MiniConsole()
        try:
            self.assertIn("Select a VM", mini.placeholder.text())
            self.assertTrue(mini.overlay.isHidden())
            mini.clear()
            self.assertIsNone(mini.domain)
            self.assertIsNone(mini._pixmap)
            self.assertTrue(mini.preview.isHidden())
        finally:
            mini.shutdown()


if __name__ == "__main__":
    unittest.main()
