from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_PYSIDE, "PySide6 is not installed")
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Import the UI package before QApplication exists so the WebEngine
        # Chromium flags are configured exactly as in vemu ui. Creating the
        # application first makes QtWebEngine segfault during interpreter
        # shutdown on some PySide6/RHEL combinations.
        from vemu.ui import configure_webengine_environment

        configure_webengine_environment()
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        cls._settings_dir = tempfile.mkdtemp(prefix="vemu-ui-settings-")
        QSettings.setPath(
            QSettings.Format.NativeFormat,
            QSettings.Scope.UserScope,
            cls._settings_dir,
        )
        cls.addClassCleanup(shutil.rmtree, cls._settings_dir, True)
        cls.app = QApplication.instance() or QApplication([])
        cls.addClassCleanup(cls._cleanup_ui)

    @classmethod
    def _cleanup_ui(cls) -> None:
        """Close and delete windows while Qt is still alive."""
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return
        for widget in app.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        app.processEvents()

    def test_action_icons_render_from_svg(self) -> None:
        from PySide6.QtGui import QImage

        from vemu.ui.icons import ICON_ASSETS, icon_pixmap

        names = {path.stem for path in ICON_ASSETS.glob("*.svg")}
        self.assertIn("play", names)
        self.assertIn("power", names)
        self.assertIn("snapshot", names)
        self.assertIn("terminal", names)
        for name in sorted(names):
            pixmap = icon_pixmap(name, "#ffffff", 18, 1.0)
            self.assertFalse(pixmap.isNull(), name)
            image = pixmap.toImage().convertToFormat(
                QImage.Format.Format_ARGB32
            )
            self.assertTrue(
                any(
                    image.pixelColor(x, y).alpha() > 0
                    for y in range(image.height())
                    for x in range(image.width())
                ),
                f"{name} rendered empty",
            )

    def test_info_badge_keeps_notes_out_of_form_rows(self) -> None:
        from vemu.ui.components import FieldLabel

        label = FieldLabel("Video", "Windows needs viogpudo", kind="alert")
        self.assertEqual(label.label.text(), "Video")
        self.assertEqual(
            label.badge.toolTip(), "Windows needs viogpudo"
        )
        self.assertFalse(label.badge.pixmap().isNull())

    def test_help_topics_cover_navigation(self) -> None:
        from vemu.ui import strings as strings
        from vemu.ui.help import HELP_TOPICS

        for label in (
            strings.NAV_LABS,
            strings.NAV_TEMPLATES,
            strings.NAV_VMS,
            strings.NAV_NETWORKS,
            strings.NAV_STORAGE,
            strings.NAV_SHARES,
            strings.NAV_GUIDE,
            strings.NAV_DOCTOR,
            strings.NAV_SETTINGS,
        ):
            self.assertIn(label, HELP_TOPICS)

    def test_form_layout_uses_uniform_grid(self) -> None:
        from vemu.ui.components import form_layout
        from vemu.ui.tokens import SPACE

        form = form_layout()
        self.assertEqual(form.verticalSpacing(), SPACE.md)
        self.assertEqual(form.horizontalSpacing(), SPACE.md)
        self.assertEqual(
            form.fieldGrowthPolicy(),
            form.FieldGrowthPolicy.AllNonFixedFieldsGrow,
        )
        self.assertEqual(
            form.rowWrapPolicy(),
            form.RowWrapPolicy.DontWrapRows,
        )

    def test_bundled_fonts_are_loaded(self) -> None:
        from PySide6.QtGui import QFontInfo

        from vemu.ui.fonts import load_bundled_fonts, mono_font, ui_font

        loaded = load_bundled_fonts()
        self.assertIn("Inter Variable", loaded)
        self.assertIn("JetBrains Mono", loaded)
        self.assertFalse(QFontInfo(ui_font()).fixedPitch())
        self.assertTrue(QFontInfo(mono_font()).fixedPitch())

    def test_main_window_shortcuts_include_map_and_console(self) -> None:
        from PySide6.QtGui import QShortcut

        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list:
                return []

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            sequences = {
                shortcut.key().toString()
                for shortcut in window.findChildren(QShortcut)
            }
            self.assertIn("Ctrl+Shift+M", sequences)
            self.assertIn("Ctrl+Shift+K", sequences)
        finally:
            window.close()

    def test_dialog_is_independent_non_modal_window(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget

        from vemu.ui.dialogs import VemuDialog

        owner = QWidget()
        dialog = VemuDialog(owner, title="Example")
        try:
            self.assertIsNone(dialog.parent())
            self.assertIs(dialog._owner, owner)
            self.assertEqual(
                dialog.windowModality(),
                Qt.WindowModality.NonModal,
            )
        finally:
            dialog.deleteLater()
            owner.deleteLater()

    def test_dialog_exec_keeps_main_window_usable(self) -> None:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from vemu.ui.dialogs import VemuDialog

        dialog = VemuDialog(title="Example")
        active_modal: list[object] = []

        def check() -> None:
            active_modal.append(QApplication.activeModalWidget())
            dialog.accept()

        QTimer.singleShot(20, check)
        try:
            self.assertEqual(int(dialog.exec()), 1)
            self.assertEqual(active_modal, [None])
        finally:
            dialog.deleteLater()

    def test_confirm_dialog_keeps_button_labels_visible(self) -> None:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import QApplication, QPushButton

        from vemu.ui.dialogs import ConfirmDialog

        dialog = ConfirmDialog(
            None,
            title="Delete VM",
            heading="Delete Demo? This cannot be undone.",
            body=(
                "Delete VM only keeps the disk chain and metadata.\n\n"
                "Delete VM and disks removes v-EMU chain directories, XML "
                "backups, the drift baseline and share assignments."
            ),
            actions=(
                ("Delete VM only", 1, "SecondaryButton"),
                ("Delete VM and disks", 2, "DangerButton"),
                ("Cancel", 0, ""),
            ),
            default_index=2,
        )
        try:
            dialog.show()
            QApplication.processEvents()
            self.assertTrue(dialog.isModal())
            self.assertEqual(
                dialog.windowModality(),
                Qt.WindowModality.ApplicationModal,
            )
            for button in dialog.findChildren(QPushButton):
                self.assertGreaterEqual(
                    button.width(), button.minimumSizeHint().width()
                )
            QTimer.singleShot(20, lambda: dialog.done(2))
            self.assertEqual(int(dialog.exec()), 2)
        finally:
            dialog.deleteLater()

    def test_sidebar_and_inspector_share_background(self) -> None:
        from vemu.ui.theme import DARK, LIGHT, stylesheet

        for tokens in (LIGHT, DARK):
            style = stylesheet(tokens)
            sidebar = style.split("QWidget#Sidebar {")[1].split("}")[0]
            inspector = style.split("QFrame#Inspector {")[1].split("}")[0]
            self.assertIn(f"background: {tokens.surface}", sidebar)
            self.assertIn(f"background: {tokens.surface}", inspector)

    def test_stylesheet_styles_message_box_buttons(self) -> None:
        from vemu.ui.theme import DARK, stylesheet

        style = stylesheet(DARK)
        self.assertIn("QMessageBox {", style)
        self.assertIn("QMessageBox QLabel {", style)
        self.assertIn("QDialogButtonBox {", style)
        self.assertIn("dialogbuttonbox-buttons-have-icons: 0", style)
        self.assertIn("QPushButton:default {", style)
        self.assertIn(f"background: {DARK.accent};", style)
        self.assertIn(f"color: {DARK.on_accent};", style)

    def test_stylesheet_checkbox_indicator_uses_asset(self) -> None:
        from vemu.ui.theme import ASSETS, DARK, LIGHT, stylesheet

        for tokens, expected in (
            (LIGHT, "check-white.svg"),
            (DARK, "check-dark.svg"),
        ):
            style = stylesheet(tokens)
            self.assertIn(expected, style)
            self.assertNotIn("file://", style)
            self.assertTrue((ASSETS / expected).is_file())
            checkbox = style.split("QCheckBox::indicator {")[1].split("}")[0]
            self.assertIn(f"background: {tokens.bg}", checkbox)

    def test_stylesheet_styles_qt_file_dialog(self) -> None:
        from vemu.ui.theme import DARK, stylesheet

        style = stylesheet(DARK)
        self.assertIn("QFileDialog QTreeView", style)
        self.assertIn("QFileDialog QHeaderView::section", style)

    def test_native_dialogs_can_be_disabled(self) -> None:
        from vemu.ui import configure_dialog_environment

        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["VEMU_NATIVE_DIALOGS"] = "0"
            configure_dialog_environment()
            self.assertNotIn("QT_QPA_PLATFORMTHEME", os.environ)

    def test_existing_platform_theme_is_kept(self) -> None:
        from vemu.ui import configure_dialog_environment

        with mock.patch.dict(
            os.environ, {"QT_QPA_PLATFORMTHEME": "customtheme"}, clear=True
        ):
            configure_dialog_environment()
            self.assertEqual(
                os.environ["QT_QPA_PLATFORMTHEME"], "customtheme"
            )

    @staticmethod
    def _fake_plugin_path(available: set[str]):
        class FakePath:
            def __init__(self, value) -> None:
                self.value = str(value)

            def is_dir(self) -> bool:
                return True

            def glob(self, pattern: str):
                if "xdgdesktopportal" in pattern and "portal" in available:
                    return ["plugin"]
                if "KDEPlasmaPlatformTheme" in pattern and "kde" in available:
                    return ["plugin"]
                return []

            def __truediv__(self, other) -> "FakePath":
                return FakePath(f"{self.value}/{other}")

        return FakePath

    def test_portal_dialog_is_preferred(self) -> None:
        from vemu.ui import configure_dialog_environment

        fake = self._fake_plugin_path({"portal", "kde"})
        with mock.patch.dict(
            os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}, clear=True
        ), mock.patch("vemu.ui.Path", fake):
            configure_dialog_environment()
            self.assertEqual(
                os.environ["QT_QPA_PLATFORMTHEME"], "xdgdesktopportal"
            )

    def test_kde_dialog_is_the_fallback(self) -> None:
        from vemu.ui import configure_dialog_environment

        fake = self._fake_plugin_path({"kde"})
        with mock.patch.dict(
            os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}, clear=True
        ), mock.patch("vemu.ui.Path", fake):
            configure_dialog_environment()
            self.assertEqual(os.environ["QT_QPA_PLATFORMTHEME"], "kde")

    def test_usb_dialog_lists_host_devices(self) -> None:
        from vemu.ui.usb import UsbDevicesDialog

        class FakeBackend:
            def usb_devices(self, domain):
                self.domain = domain
                return [
                    {
                        "name": "usb_3_2_1",
                        "vendor": "0x046d",
                        "product": "0xc077",
                        "description": "Mouse",
                        "vendor_name": "Logitech",
                        "bus": "3",
                        "device": "2",
                        "attached": False,
                        "hub": False,
                    }
                ]

            def vm_usb_attach(self, *args):
                return {}

            def vm_usb_detach(self, *args):
                return {}

        dialog = UsbDevicesDialog(FakeBackend(), "vm1")
        try:
            self.assertEqual(dialog.table.rowCount(), 1)
            dialog.table.selectRow(0)
            self.assertTrue(dialog.attach_button.isEnabled())
            self.assertFalse(dialog.detach_button.isEnabled())
        finally:
            dialog.close()

    def test_window_lists_domains(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return [
                    DomainInfo(
                        "vm1",
                        "uuid-1",
                        "running",
                        ["/tmp/disk1.qcow2"],
                        True,
                        "bios",
                        "v-EMU (full)",
                    ),
                    DomainInfo(
                        "vm2",
                        "uuid-2",
                        "stopped",
                        [],
                        False,
                        "bios",
                        "v-EMU (full)",
                    ),
                ]

            def console_info(self, name: str) -> dict:
                return {
                    "domain": name,
                    "host": "127.0.0.1",
                    "port": 5900,
                    "websocket": 5700,
                    "password": None,
                }

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=True)  # type: ignore[arg-type]
        try:
            self.assertEqual(window.table.rowCount(), 2)
            self.assertEqual(window.kpi_total.text(), "2")
            self.assertEqual(window.kpi_running.text(), "1")
            self.assertEqual(window.kpi_stopped.text(), "1")
            self.assertEqual(window.kpi_disks.text(), "1")
            self.assertEqual(window.table.item(0, 0).text(), "vm1")
            self.assertEqual(window.table.item(0, 1).text(), "Generic")
            self.assertEqual(window.table.columnCount(), 6)
            self.assertEqual(window.table.item(0, 3).text(), "v-EMU (full)")
            self.assertEqual(window.table.item(0, 4).text(), "On")
            self.assertEqual(window.table.item(1, 4).text(), "Off")

            window.table.selectRow(0)
            self.assertFalse(hasattr(window, "console_button"))
            self.assertIn("Shutdown", window.power_button.toolTip())
            self.assertTrue(window.force_button.isEnabled())
            self.assertNotIn("uuid-1", window.inspector_body.text())
            self.assertNotIn("/tmp/disk1.qcow2", window.inspector_body.text())
            window.table.selectRow(1)
            self.assertIn("Start", window.power_button.toolTip())
            self.assertFalse(window.force_button.isEnabled())

            window.table.selectRow(0)
            window._set_theme(not window.dark)
            self.assertIsNotNone(window.selected_domain)
            self.assertEqual(window.selected_domain.name, "vm1")
            self.assertEqual(window.table.currentRow(), 0)
            window.refresh()
            self.assertIsNotNone(window.selected_domain)
            self.assertEqual(window.selected_domain.name, "vm1")
            self.assertEqual(window.table.currentRow(), 0)
            window._set_auto_refresh(True, 30)
            self.assertTrue(window._auto_refresh_timer.isActive())
            self.assertEqual(window._auto_refresh_timer.interval(), 30000)
            window._set_auto_refresh(False, 30)
            self.assertFalse(window._auto_refresh_timer.isActive())
            window._set_theme(not window.dark)

            from PySide6.QtCore import Qt

            from vemu.ui.delegate import NoFocusDelegate

            self.assertIsInstance(window.table.itemDelegate(), NoFocusDelegate)
            header = window.table.horizontalHeaderItem(0)
            self.assertTrue(
                int(header.textAlignment())
                & int(Qt.AlignmentFlag.AlignLeft)
            )

            running_menu = window._build_context_menu(window.domains[0])
            running_actions = [
                action.text()
                for action in running_menu.actions()
                if not action.isSeparator()
            ]
            self.assertIn("Console", running_actions)
            self.assertIn("Shutdown", running_actions)
            self.assertIn("Delete VM…", running_actions)
            self.assertIn("Configure Share (VirtioFS)…", running_actions)
            self.assertIn("Force off", running_actions)

            stopped_menu = window._build_context_menu(window.domains[1])
            stopped_actions = [
                action.text()
                for action in stopped_menu.actions()
                if not action.isSeparator()
            ]
            self.assertIn("Start", stopped_actions)
            self.assertNotIn("Shutdown", stopped_actions)
        finally:
            window.close()

    def test_performance_tuning_dialog_and_context_action(self) -> None:
        from unittest import mock

        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow
        from vemu.ui.performance import PerformanceTuningDialog

        class FakeBackend:
            def __init__(self) -> None:
                self.applied: list[str] = []

            def list_domains(self) -> list[DomainInfo]:
                return [
                    DomainInfo(
                        name="vm-linux",
                        uuid="uuid-1",
                        state="stopped",
                        disks=["/tmp/disk.qcow2"],
                        managed_by="v-EMU (full)",
                        os_type="linux",
                    )
                ]

            def performance_probe(self, name: str) -> dict:
                return {
                    "domain": name,
                    "disk": "/tmp/disk.qcow2",
                    "supported": True,
                    "reason": "",
                    "distro": "Fedora Linux 44",
                    "os_id": "fedora",
                    "systemd": True,
                    "immutable": False,
                    "tested": True,
                    "video": "vga",
                    "needs_video_change": True,
                    "tuned": True,
                    "warnings": [],
                }

            def performance_apply(self, name: str) -> dict:
                self.applied.append(name)
                return {"xml_changed": True, "video_changed": True}

            def close(self) -> None:
                return None

        backend = FakeBackend()
        window = MainWindow(backend, dark=True)  # type: ignore[arg-type]
        try:
            window.table.selectRow(0)
            menu = window._build_context_menu(window.selected_domain)
            actions = {
                action.text(): action
                for action in menu.actions()
                if not action.isSeparator()
            }
            self.assertIn("Performance tuning…", actions)
            self.assertTrue(actions["Performance tuning…"].isEnabled())

            dialog = PerformanceTuningDialog(backend, "vm-linux", window)
            try:
                dialog._run_precheck()
                self.assertTrue(dialog.apply_button.isEnabled())
                with mock.patch(
                    "vemu.ui.performance.QMessageBox.information"
                ):
                    dialog._apply()
                self.assertTrue(dialog.changed)
                self.assertEqual(backend.applied, ["vm-linux"])
            finally:
                dialog.close()
        finally:
            window.close()

    def test_suspended_vm_offers_console_and_resume(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def __init__(self) -> None:
                self.domains = [
                    DomainInfo(
                        "vm-suspended",
                        "uuid-s",
                        "suspended",
                        ["/tmp/disk.qcow2"],
                        False,
                        "bios",
                        "v-EMU (full)",
                    )
                ]
                self.resumed: list[str] = []

            def list_domains(self) -> list[DomainInfo]:
                return self.domains

            def resume(self, name: str) -> None:
                self.resumed.append(name)
                self.domains[0].state = "running"

            def console_info(self, name: str) -> dict:
                return {
                    "domain": name,
                    "host": "127.0.0.1",
                    "port": 5902,
                    "websocket": 5702,
                    "password": None,
                }

            def close(self) -> None:
                return None

        backend = FakeBackend()
        window = MainWindow(backend, dark=True)  # type: ignore[arg-type]
        try:
            window.table.selectRow(0)
            self.assertEqual(window.table.item(0, 2).text(), "Suspended")
            self.assertIn("Resume", window.power_button.toolTip())
            self.assertTrue(window.power_button.isEnabled())
            self.assertTrue(window.force_button.isEnabled())

            menu = window._build_context_menu(window.selected_domain)
            actions = [
                action.text()
                for action in menu.actions()
                if not action.isSeparator()
            ]
            self.assertIn("Console", actions)
            self.assertIn("Resume", actions)
            self.assertIn("Force off", actions)
            self.assertNotIn("Start", actions)

            window._resume_selected()
            self.assertEqual(backend.resumed, ["vm-suspended"])
        finally:
            window.close()

    def test_console_wakes_suspended_vm_before_opening(self) -> None:
        from unittest import mock

        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def __init__(self) -> None:
                self.domains = [
                    DomainInfo(
                        "vm-suspended",
                        "uuid-s",
                        "suspended",
                        ["/tmp/disk.qcow2"],
                        False,
                        "bios",
                        "v-EMU (full)",
                    )
                ]
                self.resumed: list[str] = []

            def list_domains(self) -> list[DomainInfo]:
                return self.domains

            def resume(self, name: str) -> None:
                self.resumed.append(name)
                self.domains[0].state = "running"

            def console_info(self, name: str) -> dict:
                return {
                    "domain": name,
                    "host": "127.0.0.1",
                    "port": 5902,
                    "websocket": 5702,
                    "password": None,
                }

            def close(self) -> None:
                return None

        backend = FakeBackend()
        window = MainWindow(backend, dark=True)  # type: ignore[arg-type]
        try:
            window.table.selectRow(0)
            with mock.patch("vemu.ui.console.ConsoleWindow") as console_cls:
                window._open_console()
            self.assertEqual(backend.resumed, ["vm-suspended"])
            console_cls.assert_called_once()
            console_cls.return_value.show.assert_called_once()
        finally:
            window.close()

    def test_setup_banner_after_skipped_onboarding(self) -> None:
        from PySide6.QtCore import QSettings

        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list:
                return []

            def storage_list(self) -> list:
                return []

            def networks_list(self) -> list:
                return []

            def close(self) -> None:
                return None

        settings = QSettings("v-EMU", "v-EMU")
        old_skipped = settings.value(
            "onboarding/skipped", False, type=bool
        )
        old_dismissed = settings.value(
            "onboarding/banner_dismissed", False, type=bool
        )
        settings.setValue("onboarding/skipped", True)
        settings.setValue("onboarding/banner_dismissed", False)
        window = MainWindow(FakeBackend(), dark=True)  # type: ignore[arg-type]
        try:
            window.refresh()
            self.assertFalse(window.setup_banner.isHidden())
            text = window.setup_banner_label.text()
            self.assertIn("storage", text)
            self.assertIn("NAT", text)
            self.assertIn("No virtual machines yet", window.table._empty_title.text())
            self.assertIn("storage", window.table._empty_body.text())
            window._dismiss_setup_banner()
            self.assertTrue(window.setup_banner.isHidden())
            self.assertTrue(
                settings.value(
                    "onboarding/banner_dismissed", False, type=bool
                )
            )
            window._labs_ready = True
            window._has_nat = True
            window._update_setup_banner()
            self.assertTrue(window.setup_banner.isHidden())
            self.assertFalse(
                settings.value("onboarding/skipped", True, type=bool)
            )
        finally:
            window.close()
            settings.setValue("onboarding/skipped", old_skipped)
            settings.setValue(
                "onboarding/banner_dismissed", old_dismissed
            )

    def test_vm_table_sorts_by_headers(self) -> None:
        from PySide6.QtCore import Qt

        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return [
                    DomainInfo(
                        "beta", "u1", "running", ["/d1", "/d2"], False,
                        "bios", "v-EMU (full)", os_type="windows"
                    ),
                    DomainInfo(
                        "alpha", "u2", "stopped", ["/d1"], True,
                        "bios", "v-EMU (full)", os_type="linux"
                    ),
                    DomainInfo(
                        "gamma", "u3", "paused", [], True,
                        "bios", "v-EMU (full)", os_type="hypervisor"
                    ),
                ]

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            table = window.table
            names = lambda: [
                table.item(row, 0).text() for row in range(table.rowCount())
            ]
            table.sortItems(0, Qt.SortOrder.AscendingOrder)
            self.assertEqual(names(), ["alpha", "beta", "gamma"])
            table.sortItems(5, Qt.SortOrder.DescendingOrder)
            self.assertEqual(names(), ["beta", "alpha", "gamma"])
            table.sortItems(2, Qt.SortOrder.AscendingOrder)
            self.assertEqual(names(), ["beta", "gamma", "alpha"])
            table.sortItems(4, Qt.SortOrder.AscendingOrder)
            self.assertEqual(names(), ["alpha", "gamma", "beta"])
            table.sortItems(1, Qt.SortOrder.AscendingOrder)
            self.assertEqual(names(), ["beta", "alpha", "gamma"])
        finally:
            window.close()

    def test_external_vm_is_read_only(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return [
                    DomainInfo(
                        "cockpit-vm", "uuid-1", "running", ["/d1"],
                        False, "bios", "External"
                    )
                ]

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            window.table.selectRow(0)
            self.assertEqual(window.table.item(0, 3).text(), "External")
            self.assertFalse(window.power_button.isEnabled())
            menu = window._build_context_menu(window.selected_domain)
            actions = [
                action.text() for action in menu.actions()
                if not action.isSeparator()
            ]
            self.assertIn("Console", actions)
            self.assertIn("Adopt / Migrate to v-EMU…", actions)
            self.assertIn("Rescue shutdown…", actions)
            self.assertIn("External · read-only", actions)
            self.assertNotIn("Shutdown", actions)
            self.assertNotIn("Configure hardware…", actions)
        finally:
            window.close()

    def test_drifted_vm_shows_drift_action(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return [
                    DomainInfo(
                        "vm1", "uuid-1", "stopped", ["/d1"], False,
                        "bios", "v-EMU (full)", "drifted"
                    )
                ]

            def domain_drift(self, _name: str) -> dict:
                return {
                    "status": "drifted",
                    "changes": ["domain/devices/disk[1]/source/@file: a -> b"],
                }

            def domain_baseline_refresh(self, _name: str) -> dict:
                return {"refreshed": ["vm1"]}

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            window.table.selectRow(0)
            menu = window._build_context_menu(window.selected_domain)
            actions = [
                action.text() for action in menu.actions()
                if not action.isSeparator()
            ]
            self.assertIn("Show XML drift…", actions)
            self.assertIn("Re-pin XML baseline…", actions)
        finally:
            window.close()

    def test_migrate_dialog_gates_snapshot_and_space(self) -> None:
        from vemu.ui.migrate import MigrateDialog

        class FakeBackend:
            pass

        dialog = MigrateDialog(FakeBackend(), "vm1", "External")
        try:
            plan = {
                "supported": True,
                "sufficient": True,
                "snapshot_loss_required": True,
                "snapshots": ["SS0"],
                "disks": [
                    {
                        "target": "vda",
                        "source": "/tmp/old.qcow2",
                        "required_human": "10.0 GiB",
                    }
                ],
                "estimated_human": "10.0 GiB",
                "headroom_bytes": 2 * 1024**3,
                "required_human": "11.5 GiB",
                "available_human": "20.0 GiB",
                "destination": "/tmp/labs/vm1",
                "firmware": None,
                "new_firmware": False,
                "reasons": [],
            }
            dialog.plan = plan
            dialog._populate()
            dialog._update_gate()
            self.assertFalse(dialog.migrate_button.isEnabled())
            dialog.snapshot_check.setChecked(True)
            self.assertTrue(dialog.migrate_button.isEnabled())

            plan["sufficient"] = False
            plan["reasons"] = ["Insufficient space"]
            dialog._populate()
            dialog._update_gate()
            self.assertFalse(dialog.migrate_button.isEnabled())
            self.assertTrue(dialog.warning_label.isVisibleTo(dialog))
        finally:
            dialog.deleteLater()

    def test_migrate_dialog_offers_snapshot_import(self) -> None:
        from PySide6.QtCore import QSignalBlocker
        from vemu.ui.migrate import MigrateDialog

        class FakeBackend:
            pass

        dialog = MigrateDialog(FakeBackend(), "vm1", "External")
        try:
            plan = {
                "supported": True,
                "sufficient": True,
                "snapshot_loss_required": False,
                "snapshots": ["SS0", "SS1"],
                "snapshot_import": {
                    "supported": True,
                    "layer_count": 3,
                    "reason": "",
                },
                "disks": [],
                "estimated_human": "1.0 GiB",
                "headroom_bytes": 0,
                "required_human": "1.1 GiB",
                "available_human": "10.0 GiB",
                "destination": "/tmp/labs/vm1",
                "reasons": [],
            }
            dialog.plan = plan
            dialog._populate()
            self.assertTrue(dialog.import_check.isVisibleTo(dialog))
            self.assertTrue(dialog.import_check.isEnabled())
            self.assertIn("2 existing snapshot", dialog.import_check.text())
            with QSignalBlocker(dialog.import_check):
                dialog.import_check.setChecked(True)
            dialog._update_gate()
            self.assertTrue(dialog.migrate_button.isEnabled())
        finally:
            dialog.deleteLater()

    def test_import_button_has_ova_and_disk_actions(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            menu = window.import_button.menu()
            self.assertIsNotNone(menu)
            actions = [action.text() for action in menu.actions()]
            self.assertIn("OVA appliance…", actions)
            self.assertIn("Disk image…", actions)
        finally:
            window.close()

    def test_clone_button_opens_templates(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def storage_list(self) -> list[dict]:
                return [
                    {"path": "/tmp/labs", "role": "labs"},
                    {"path": "/tmp/templates", "role": "templates"},
                ]

            def templates_list(self, root: str) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            self.assertTrue(window.clone_button.isEnabled())
            window.clone_button.click()
            self.assertIs(window.stack.currentWidget(), window.templates_page)
        finally:
            window.close()

    def test_search_filters_rows(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return [
                    DomainInfo("alpha", "uuid-1", "stopped", []),
                    DomainInfo("beta", "uuid-2", "stopped", []),
                ]

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            window.search.setText("alp")
            self.assertFalse(window.table.isRowHidden(0))
            self.assertTrue(window.table.isRowHidden(1))
        finally:
            window.close()

    def test_collapsible_panes(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            self.assertIsNotNone(window.sidebar_button)
            was_sidebar = window.ui_sidebar_visible
            was_inspector = window.ui_inspector_visible
            window.ui_sidebar_visible = True
            window.ui_inspector_visible = True
            window._apply_pane_state()
            self.assertFalse(window.sidebar.isHidden())
            self.assertFalse(window.inspector.isHidden())

            window._toggle_sidebar()
            self.assertTrue(window.sidebar.isHidden())
            window._toggle_sidebar()
            self.assertFalse(window.sidebar.isHidden())

            window._toggle_inspector()
            self.assertTrue(window.inspector.isHidden())
            window._toggle_inspector()
            self.assertFalse(window.inspector.isHidden())

            window.ui_sidebar_visible = was_sidebar
            window.ui_inspector_visible = was_inspector
            window._apply_pane_state()
            window._save_ui_state()
        finally:
            window.close()

    def test_guest_stats_do_not_show_rss_above_guest_ram(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def __init__(self) -> None:
                self.stats = {
                    "name": "vm1",
                    "state": "running",
                    "vcpus": 6,
                    "cpu_time_ns": 1000,
                    "memory_rss_mib": 9031,
                    "memory_actual_mib": 8192,
                }

            def list_domains(self) -> list[DomainInfo]:
                return [
                    DomainInfo(
                        name="vm1",
                        uuid="uuid-1",
                        state="running",
                        disks=[],
                        autostart=False,
                        firmware="efi",
                    )
                ]

            def domain_stats(self, _name: str) -> dict:
                return dict(self.stats)

            def close(self) -> None:
                return None

        backend = FakeBackend()
        window = MainWindow(backend, dark=False)  # type: ignore[arg-type]
        try:
            window.table.selectRow(0)
            window._refresh_guest_stats()
            self.assertIn(
                "RAM 8192 MiB allocated", window.inspector_stats.text()
            )
            self.assertNotIn("9031/8192", window.inspector_stats.text())
            self.assertIn("9031", window.inspector_stats.toolTip())
            self.assertIn("viomem", window.inspector_stats.toolTip())

            backend.stats["memory_available_mib"] = 8192
            backend.stats["memory_usable_mib"] = 4096
            window._refresh_guest_stats()
            self.assertIn("RAM 4096/8192 MiB", window.inspector_stats.text())
            self.assertIn("usable", window.inspector_stats.toolTip())
        finally:
            window.close()

    def test_icon_buttons_are_uniform(self) -> None:
        from PySide6.QtCore import QSize

        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            self.assertGreaterEqual(len(window._icon_buttons), 4)
            for button in window._icon_buttons:
                self.assertEqual(button.text(), "")
                self.assertEqual(button.iconSize(), QSize(18, 18))
                expected = (
                    34
                    if button.objectName() == "InspectorAction"
                    else 32
                )
                self.assertEqual(button.width(), expected)
                self.assertEqual(button.height(), expected)
                self.assertFalse(button.icon().isNull())
            window._toggle_theme()
            self.assertTrue(window.dark)
            for button in window._icon_buttons:
                self.assertFalse(button.icon().isNull())
            from PySide6.QtWidgets import QApplication

            self.assertIn(
                "QMenu", QApplication.instance().styleSheet()
            )
            self.assertIn("QToolTip", QApplication.instance().styleSheet())
            style = QApplication.instance().styleSheet()
            self.assertEqual(style.count("QToolTip {"), 1)
            self.assertIn("border-radius: 6px", style)
        finally:
            window.dark = False
            window.close()

    def test_reservation_dialog(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from vemu.ui.network_page import DhcpReservationDialog

        calls: list[tuple[str, str]] = []

        class FakeBackend:
            def networks_list(self) -> list[dict]:
                return [
                    {
                        "name": "lab-nat",
                        "dhcp": True,
                        "host_reservations": [
                            {
                                "mac": "52:54:00:aa:bb:cc",
                                "ip": "10.213.0.20",
                                "hostname": "router",
                            }
                        ],
                    }
                ]

            def network_reservation_remove(
                self, name: str, mac: str
            ) -> dict:
                calls.append((name, mac))
                return {"profile": {"name": name}}

        dialog = DhcpReservationDialog(FakeBackend(), {"name": "lab-nat"})
        try:
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(
                dialog.table.item(0, 1).text(), "10.213.0.20"
            )
            dialog.table.selectRow(0)
            self.assertTrue(dialog.remove_button.isEnabled())
            with mock.patch(
                "vemu.ui.network_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog._remove()
            self.assertEqual(calls, [("lab-nat", "52:54:00:aa:bb:cc")])
            self.assertTrue(dialog.changed)
        finally:
            dialog.close()

    def test_mini_console_schedule(self) -> None:
        from PySide6.QtCore import Signal
        from PySide6.QtWidgets import QWidget

        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return [
                    DomainInfo("vm1", "uuid-1", "running", []),
                    DomainInfo("vm2", "uuid-2", "stopped", []),
                ]

            def console_info(self, name: str) -> dict:
                return {
                    "domain": name,
                    "host": "127.0.0.1",
                    "port": 5900,
                    "websocket": 5700,
                }

            def close(self) -> None:
                return None

        class FakeMini(QWidget):
            opened = Signal()

            def __init__(self, parent=None, backend=None) -> None:
                super().__init__(parent)
                self.backend = backend
                self.domain = None
                self.loaded = None
                self.cleared = False
                self.no_signal = None

            def set_theme(self, _tokens) -> None:
                return None

            def load(self, info: dict) -> None:
                self.loaded = info
                self.domain = info.get("domain")
                self.no_signal = None

            def show_no_signal(self, domain=None) -> None:
                self.domain = domain
                self.no_signal = domain

            def clear(self) -> None:
                self.cleared = True
                self.loaded = None
                self.domain = None

            def shutdown(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            window.table.selectRow(0)
            with mock.patch("vemu.ui.console.MiniConsole", FakeMini):
                window._refresh_mini_console_now()
            self.assertIsNotNone(window.mini_console)
            self.assertEqual(
                window.mini_console.loaded["domain"], "vm1"
            )
            window.table.selectRow(1)
            window._refresh_mini_console_now()
            self.assertEqual(window.mini_console.no_signal, "vm2")
        finally:
            window.close()

    def test_new_vm_dialog(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        captured: dict = {}

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return [{"path": "/tmp/labs", "role": "labs"}]

            def networks_list(self) -> list[dict]:
                return [{"name": "lab-nat", "mode": "nat"}]

            def vm_create(self, **params) -> dict:
                captured.update(params)
                return {
                    "name": params["name"],
                    "admin_password": params["admin_password"],
                    "windows_edition_label": "Windows Server 2025 Standard (Desktop Experience)",
                }

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.name_edit.setText("win-lab")
            dialog.iso_edit.setText("/home/ISO/server.iso")
            dialog.dest_edit.setText("/tmp/labs")
            dialog.password_edit.setText("Vemu!Lab2026")
            dialog._create()
            self.assertEqual(captured["name"], "win-lab")
            self.assertEqual(captured["os_type"], "windows")
            self.assertEqual(captured["os_variant"], "windows-server-2025")
            self.assertTrue(captured["windows_unattend"])
            self.assertTrue(captured["start"])
            self.assertEqual(captured["firmware"], "bios")
            self.assertEqual(captured["machine"], "q35")
            self.assertEqual(captured["nic_model"], "e1000e")
            self.assertEqual(captured["video"], "vga")
            self.assertEqual(captured["video_memory_mib"], 64)
            self.assertTrue(dialog.video_memory_combo.isEnabled())
            self.assertIn("viogpudo", dialog.video_badge.toolTip())
            dialog.video_combo.setCurrentIndex(
                dialog.video_combo.findData("virtio")
            )
            self.assertIn("WDDM", dialog.video_badge.toolTip())
            self.assertFalse(dialog.video_memory_combo.isEnabled())
            self.assertEqual(dialog.created["name"], "win-lab")
        finally:
            dialog.close()

    def test_new_vm_driver_iso_is_windows_only_and_optional(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        captured: dict = {}

        class FakeBackend:
            def __init__(self, tmp: str) -> None:
                self.tmp = tmp

            def storage_list(self) -> list[dict]:
                return [
                    {"path": self.tmp, "role": "isos"},
                    {"path": self.tmp, "role": "labs"},
                ]

            def networks_list(self) -> list[dict]:
                return [{"name": "lab-nat", "mode": "nat"}]

            def vm_create(self, **params) -> dict:
                captured.update(params)
                return {"name": params["name"]}

        with tempfile.TemporaryDirectory() as tmp:
            # A virtio-win.iso in the ISO location must not be auto-filled.
            (Path(tmp) / "virtio-win.iso").write_bytes(b"")
            dialog = NewVmDialog(FakeBackend(tmp))
            try:
                self.assertEqual(dialog.virtio_edit.text(), "")
                self.assertEqual(dialog.virtio_edit.placeholderText(), "")
                margins = dialog.virtio_row.layout().contentsMargins()
                self.assertEqual(
                    (
                        margins.left(),
                        margins.top(),
                        margins.right(),
                        margins.bottom(),
                    ),
                    (0, 0, 0, 0),
                )
                self.assertFalse(dialog.virtio_row.isHidden())
                dialog.family_combo.setCurrentIndex(
                    dialog.family_combo.findData("linux")
                )
                self.assertTrue(dialog.virtio_row.isHidden())
                self.assertTrue(dialog.virtio_label.isHidden())
                self.assertIn("No driver CD", dialog.storage_note.text())
                dialog.family_combo.setCurrentIndex(
                    dialog.family_combo.findData("windows")
                )
                self.assertFalse(dialog.virtio_row.isHidden())

                # A hidden stale path must not be attached to a Linux VM.
                dialog.family_combo.setCurrentIndex(
                    dialog.family_combo.findData("linux")
                )
                dialog.virtio_edit.setText(str(Path(tmp) / "virtio-win.iso"))
                dialog.name_edit.setText("linux-lab")
                dialog.iso_edit.setText(
                    "/home/ISO/Fedora-Workstation-44.iso"
                )
                dialog.dest_edit.setText(tmp)
                dialog._create()
                self.assertIsNone(captured["virtio_iso"])
            finally:
                dialog.close()

    def test_new_vm_memory_uses_gigabytes_with_decimals(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        captured: dict = {}

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return [{"name": "lab-nat", "mode": "nat"}]

            def vm_create(self, **params) -> dict:
                captured.update(params)
                return {"name": params["name"]}

        dialog = NewVmDialog(FakeBackend())
        try:
            self.assertEqual(dialog.memory_spin.suffix().strip(), "GB")
            dialog.name_edit.setText("small-linux")
            dialog.iso_edit.setText("/home/ISO/Fedora-Workstation-44.iso")
            dialog._detect_iso()
            dialog.dest_edit.setText("/tmp/labs")
            dialog.memory_spin.setValue(1.5)
            dialog._create()
            self.assertEqual(captured["memory"], 1536)
        finally:
            dialog.close()

    def test_new_vm_manual_memory_survives_iso_detection(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.memory_spin.setValue(5.0)
            dialog.iso_edit.setText(
                "/home/ISO/alpine-standard-3.24.3-aarch64.iso"
            )
            dialog._detect_iso()
            self.assertEqual(dialog.family_combo.currentData(), "linux")
            self.assertEqual(dialog.memory_spin.value(), 5.0)
        finally:
            dialog.close()

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog._hardware_visited = True
            dialog.iso_edit.setText(
                "/home/ISO/alpine-standard-3.24.3-aarch64.iso"
            )
            dialog._detect_iso()
            self.assertEqual(dialog.memory_spin.value(), 4.0)
        finally:
            dialog.close()

    def test_cross_arch_setting_gates_the_wizard(self) -> None:
        from PySide6.QtCore import QSettings

        from vemu.ui.vm_create import NewVmDialog

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

            def vm_capabilities(self, arch: str = "x86_64") -> dict:
                return {
                    "arches": [
                        {"name": "x86_64"},
                        {"name": "aarch64"},
                        {"name": "riscv64"},
                    ]
                }

        settings = QSettings("v-EMU", "v-EMU")
        try:
            settings.remove("ui/cross_arch")
            dialog = NewVmDialog(FakeBackend())
            try:
                self.assertEqual(dialog.arch_combo.count(), 1)
                self.assertEqual(dialog.arch_combo.currentData(), "x86_64")
            finally:
                dialog.close()
            settings.setValue("ui/cross_arch", False)
            dialog = NewVmDialog(FakeBackend())
            try:
                self.assertEqual(dialog.arch_combo.count(), 1)
                self.assertEqual(dialog.arch_combo.currentData(), "x86_64")
            finally:
                dialog.close()
            settings.setValue("ui/cross_arch", True)
            dialog = NewVmDialog(FakeBackend())
            try:
                names = [
                    dialog.arch_combo.itemData(i)
                    for i in range(dialog.arch_combo.count())
                ]
                self.assertIn("aarch64", names)
                self.assertIn("riscv64", names)
            finally:
                dialog.close()
        finally:
            settings.setValue("ui/cross_arch", False)

    def test_new_vm_detects_linux_iso(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.iso_edit.setText("/home/ISO/Fedora-Workstation-44.iso")
            dialog._detect_iso()
            self.assertEqual(dialog.family_combo.currentData(), "linux")
            self.assertEqual(dialog.variant_combo.currentData(), "fedora")
            self.assertFalse(dialog.unattend_check.isChecked())
            self.assertFalse(dialog.edition_combo.isEnabled())
            self.assertFalse(dialog.password_edit.isEnabled())
            self.assertEqual(dialog.disk_bus_combo.currentData(), "virtio")
            self.assertGreaterEqual(dialog.disk_bus_combo.findData("nvme"), 0)
            self.assertEqual(dialog.nic_combo.currentData(), "virtio")
            self.assertEqual(dialog.video_combo.currentData(), "vga")
        finally:
            dialog.close()


    def test_combo_boxes_use_stable_height(self) -> None:
        from PySide6.QtWidgets import QComboBox

        from vemu.ui.widgets import FIELD_HEIGHT, ComboBox

        combo = ComboBox()
        try:
            combo.addItem("riscv64")
            self.assertGreaterEqual(combo.minimumHeight(), FIELD_HEIGHT)
            self.assertEqual(
                combo.sizeAdjustPolicy(),
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon,
            )
        finally:
            combo.deleteLater()

    def test_page_header_subtitle_stays_on_one_line(self) -> None:
        from vemu.ui.components import PageHeader

        header = PageHeader(
            "Networks",
            "Lab segments, DHCP/DNS, and NAT port forwarding.",
        )
        try:
            self.assertFalse(header.subtitle.wordWrap())
        finally:
            header.deleteLater()

    def test_network_dialog_edits_existing_profile(self) -> None:
        from vemu.ui.network_page import NetworkAddDialog

        captured: dict = {}

        class FakeBackend:
            def network_update(
                self,
                name: str,
                mode: str,
                subnet: str,
                dhcp: bool,
                dns_domain: str,
                description: str = "",
            ) -> dict:
                captured.update(
                    name=name,
                    mode=mode,
                    subnet=subnet,
                    dhcp=dhcp,
                    dns_domain=dns_domain,
                    description=description,
                )
                return {"name": name}

        profile = {
            "name": "lab-nat",
            "mode": "nat",
            "subnet": "10.213.0.0/24",
            "dhcp": True,
            "dns_domain": "lab.local",
            "description": "classroom",
            "bridge": "",
        }
        dialog = NetworkAddDialog(FakeBackend(), profile=profile)
        try:
            self.assertFalse(dialog.name_edit.isEnabled())
            self.assertFalse(dialog.mode_combo.isEnabled())
            self.assertEqual(dialog.subnet_edit.text(), "10.213.0.0/24")
            dialog.subnet_edit.setText("10.214.0.0/24")
            dialog._create()
            self.assertEqual(captured["name"], "lab-nat")
            self.assertEqual(captured["subnet"], "10.214.0.0/24")
            self.assertEqual(captured["description"], "classroom")
            self.assertIsNotNone(dialog.profile)
        finally:
            dialog.close()

    def test_lan_bridge_wizard_registers_existing_bridge(self) -> None:
        from vemu.ui.network_page import LanBridgeWizard

        captured: dict = {}
        applied: list[bool] = []

        class FakeBackend:
            def network_bridge_inventory(self) -> dict:
                return {
                    "nmcli": True,
                    "bridges": [
                        {
                            "name": "bridge0",
                            "state": "up",
                            "addresses": ["192.168.80.1/24"],
                            "interfaces": ["enp3s0"],
                        }
                    ],
                    "uplinks": [],
                }

            def network_add(self, *args) -> dict:
                captured["add"] = args
                return {"name": args[0]}

            def network_apply(self) -> dict:
                applied.append(True)
                return {}

        dialog = LanBridgeWizard(FakeBackend())
        try:
            self.assertEqual(
                dialog.existing_combo.currentData(), "bridge0"
            )
            self.assertEqual(
                dialog.mode_combo.currentData(), "existing"
            )
            dialog.profile_edit.setText("lab-lan")
            dialog.description_edit.setText("desk LAN")
            dialog._create()
            self.assertEqual(captured["add"][0], "lab-lan")
            self.assertEqual(captured["add"][1], "bridge")
            self.assertEqual(captured["add"][2], "auto")
            self.assertFalse(captured["add"][3])
            self.assertEqual(captured["add"][6], "bridge0")
            self.assertEqual(applied, [True])
            self.assertFalse(dialog.created_host_bridge)
            self.assertEqual(dialog.bridge_name, "bridge0")
        finally:
            dialog.close()

    def test_lan_bridge_wizard_creates_bridge_and_profile(self) -> None:
        from vemu.ui.network_page import LanBridgeWizard

        captured: dict = {}
        applied: list[bool] = []

        class FakeBackend:
            def network_bridge_inventory(self) -> dict:
                return {
                    "nmcli": True,
                    "bridges": [],
                    "uplinks": [
                        {
                            "name": "enp4s0",
                            "state": "up",
                            "addresses": ["10.0.0.20/24"],
                            "connection": {"name": "Office LAN"},
                        }
                    ],
                }

            def network_bridge_create(self, *args) -> dict:
                captured["create"] = args
                return {
                    "name": args[1],
                    "profile": {"name": args[0]},
                }

            def network_apply(self) -> dict:
                applied.append(True)
                return {}

        dialog = LanBridgeWizard(FakeBackend())
        try:
            self.assertEqual(dialog.mode_combo.currentData(), "create")
            self.assertEqual(dialog.uplink_combo.currentData(), "enp4s0")
            dialog.profile_edit.setText("lab-lan")
            dialog.bridge_edit.setText("br-lab")
            dialog.addressing_combo.setCurrentIndex(
                dialog.addressing_combo.findData("static")
            )
            self.assertTrue(dialog.address_edit.isVisibleTo(dialog))
            dialog.address_edit.setText("192.168.1.50/24")
            dialog.gateway_edit.setText("192.168.1.1")
            dialog.dns_edit.setText("1.1.1.1")
            dialog._create()
            self.assertEqual(captured["create"][0], "lab-lan")
            self.assertEqual(captured["create"][1], "br-lab")
            self.assertEqual(captured["create"][2], "enp4s0")
            self.assertEqual(captured["create"][3], "static")
            self.assertEqual(captured["create"][4], "192.168.1.50/24")
            self.assertEqual(applied, [True])
            self.assertTrue(dialog.created_host_bridge)
            self.assertEqual(dialog.bridge_name, "br-lab")
        finally:
            dialog.close()

    def test_lan_bridge_wizard_marks_bridge_without_uplink(self) -> None:
        from vemu.ui.network_page import LanBridgeWizard

        class FakeBackend:
            def network_bridge_inventory(self) -> dict:
                return {
                    "nmcli": True,
                    "bridges": [
                        {
                            "name": "virbr0",
                            "state": "down",
                            "addresses": [],
                            "interfaces": [],
                        }
                    ],
                    "uplinks": [],
                }

            def network_add(self, *args) -> dict:
                return {"name": args[0]}

            def network_apply(self) -> dict:
                return {}

        dialog = LanBridgeWizard(FakeBackend())
        try:
            self.assertIn("no physical uplink", dialog.warning.text())
        finally:
            dialog.close()

    def test_storage_files_dialog_marks_orphans(self) -> None:
        from vemu.ui.storage_page import StorageFilesDialog

        class FakeBackend:
            def storage_files(self, _path: str) -> dict:
                return {
                    "files": [
                        {
                            "path": "/media/orphan.iso",
                            "name": "orphan.iso",
                            "kind": "iso",
                            "size": 1024,
                            "status": "available",
                            "deletable": True,
                        },
                        {
                            "path": "/media/used.qcow2",
                            "name": "used.qcow2",
                            "kind": "image",
                            "size": 2048,
                            "status": "in use by vm1",
                            "deletable": False,
                        },
                    ],
                    "chains": [],
                }

        dialog = StorageFilesDialog(
            FakeBackend(), {"path": "/media", "role": "isos"}
        )
        try:
            self.assertEqual(dialog.table.rowCount(), 2)
            dialog.table.selectRow(0)
            self.assertTrue(dialog.delete_button.isEnabled())
            dialog.table.selectRow(1)
            self.assertFalse(dialog.delete_button.isEnabled())
        finally:
            dialog.close()

    def test_storage_garbage_dialog_deletes_only_checked_items(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from vemu.ui.storage_page import StorageGarbageDialog

        calls: list[str] = []

        class FakeBackend:
            def storage_delete_file(self, path: str) -> dict:
                calls.append(path)
                return {"path": path, "deleted": True}

        entries = [
            {
                "path": "/media/orphan.iso",
                "name": "orphan.iso",
                "kind": "iso",
                "size": 1024,
                "status": "available",
                "deletable": True,
            },
            {
                "path": "/media/used.qcow2",
                "name": "used.qcow2",
                "kind": "image",
                "size": 2048,
                "status": "in use by vm1",
                "deletable": False,
            },
        ]
        dialog = StorageGarbageDialog(
            FakeBackend(), {"path": "/media", "role": "labs"}, entries
        )
        try:
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertFalse(dialog.delete_button.isEnabled())
            dialog.table.item(0, 0).setCheckState(
                Qt.CheckState.Checked
            )
            self.assertTrue(dialog.delete_button.isEnabled())
            with mock.patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ):
                dialog._delete()
            self.assertEqual(calls, ["/media/orphan.iso"])
            self.assertEqual(dialog.deleted, 1)
        finally:
            dialog.close()

    def test_disk_add_dialog_is_staged_and_uses_unique_path(self) -> None:
        from vemu.ui.vm_config import DiskAddDialog

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data-disk-1.qcow2").write_bytes(b"x")
            calls: list[dict] = []

            class FakeBackend:
                def vm_disk_add(self, **kwargs):
                    calls.append(kwargs)
                    return {"disk": {"source": kwargs["path"]}}

            dialog = DiskAddDialog(FakeBackend(), "vm1", str(root))
            try:
                self.assertTrue(
                    dialog.path_edit.text().endswith("data-disk-2.qcow2")
                )
                dialog.size_edit.setText("5")
                dialog._add()
                self.assertEqual(calls, [])
                self.assertIsNotNone(dialog.spec)
                self.assertTrue(dialog.spec["create_file"])
                self.assertEqual(dialog.spec["size_gb"], 5)
            finally:
                dialog.close()

    def test_disk_add_dialog_can_attach_existing_image(self) -> None:
        from vemu.ui.vm_config import DiskAddDialog

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "existing.qcow2"
            existing.write_bytes(b"x")

            class FakeBackend:
                def vm_disk_add(self, **kwargs):
                    raise AssertionError("must be staged")

            dialog = DiskAddDialog(FakeBackend(), "vm1", str(root))
            try:
                dialog.path_edit.setText(str(existing))
                dialog.existing_check.setChecked(True)
                dialog._add()
                self.assertIsNotNone(dialog.spec)
                self.assertFalse(dialog.spec["create_file"])
                self.assertEqual(dialog.spec["size_gb"], 0)
            finally:
                dialog.close()

    def test_drift_dialog_shows_changes_and_repins(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from vemu.ui.drift import DriftDialog

        calls: list[str] = []

        class FakeBackend:
            def domain_drift(self, name: str) -> dict:
                return {
                    "status": "drifted",
                    "baseline": "old",
                    "current": "new",
                    "changes": ["domain/memory: 256 -> 512"],
                }

            def domain_baseline_refresh(self, name: str) -> dict:
                calls.append(name)
                return {"refreshed": [name]}

        dialog = DriftDialog(FakeBackend(), ["vm1"])
        try:
            self.assertIn("domain/memory", dialog.text.toPlainText())
            with mock.patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ), mock.patch.object(QMessageBox, "information"):
                dialog._repin()
            self.assertEqual(calls, ["vm1"])
        finally:
            dialog.close()

    def test_new_vm_cpu_modes_exclude_custom_and_tcg_maximum(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog._update_combo_options(
                {
                    "cpu_modes": [
                        "host-passthrough",
                        "host-model",
                        "custom",
                        "maximum",
                    ],
                    "disk_buses": ["virtio", "sata"],
                    "video_models": ["virtio"],
                    "nic_models": ["virtio"],
                },
                "x86_64",
            )
            values = [
                dialog.cpu_combo.itemData(i)
                for i in range(dialog.cpu_combo.count())
            ]
            self.assertEqual(values, ["host-passthrough", "host-model"])
            dialog._update_combo_options(
                {
                    "cpu_modes": ["maximum", "custom"],
                    "disk_buses": ["virtio"],
                    "video_models": ["virtio"],
                    "nic_models": ["virtio"],
                },
                "aarch64",
            )
            values = [
                dialog.cpu_combo.itemData(i)
                for i in range(dialog.cpu_combo.count())
            ]
            self.assertEqual(values, ["maximum"])
        finally:
            dialog.close()

    def test_doctor_exposes_xml_drift_review(self) -> None:
        from vemu.ui.doctor_page import DoctorPage

        class FakeBackend:
            def domain_drift(self, _name: str) -> dict:
                return {"status": "drifted", "changes": ["x"]}

        page = DoctorPage(FakeBackend())
        try:
            page.payload = {
                "ok": True,
                "checks": [
                    {
                        "check": "domain_drift",
                        "level": "warn",
                        "detail": "XML drifted from baseline: vm1",
                        "domains": ["vm1"],
                    }
                ],
            }
            page._fill()
            self.assertFalse(page.drift_button.isHidden())
            self.assertIn("vm1", page.drift_label.text())
        finally:
            page.close()

    def test_new_vm_aarch64_options_are_selectable(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.arch_combo.addItem("aarch64", "aarch64")
            dialog.arch_combo.setCurrentIndex(
                dialog.arch_combo.findData("aarch64")
            )
            dialog._arch_changed()
            self.assertTrue(dialog.tpm_check.isEnabled())
            self.assertEqual(dialog.iso_bus_combo.currentData(), "usb")
            self.assertGreaterEqual(dialog.disk_bus_combo.findData("usb"), 0)
            self.assertGreaterEqual(dialog.nic_combo.findData("e1000e"), 0)
            self.assertEqual(dialog.firmware_combo.currentData(), "efi")
            self.assertEqual(dialog.disk_bus_combo.itemData(0), "virtio")
            self.assertGreaterEqual(dialog.disk_bus_combo.findData("nvme"), 0)
            self.assertEqual(dialog.video_combo.currentData(), "ramfb")
            self.assertGreaterEqual(dialog.memory_spin.value(), 4.0)
        finally:
            dialog.close()

    def test_new_vm_windows_client_is_manual(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.iso_edit.setText("/home/ISO/Win11_24H2_English_x64.iso")
            dialog._detect_iso()
            self.assertEqual(dialog.family_combo.currentData(), "windows")
            self.assertEqual(dialog.variant_combo.currentData(), "windows-11")
            self.assertFalse(dialog.unattend_check.isChecked())
            self.assertEqual(dialog.disk_bus_combo.currentData(), "sata")
            self.assertEqual(dialog.nic_combo.currentData(), "e1000e")
            self.assertEqual(dialog.video_combo.currentData(), "vga")
            self.assertEqual(dialog.firmware_combo.currentData(), "efi")
            self.assertTrue(dialog.tpm_check.isChecked())
            self.assertEqual(dialog.memory_spin.value(), 4.0)
            self.assertEqual(dialog.disk_edit.text(), "64")
            self.assertEqual(dialog.detected_label.objectName(), "Muted")
        finally:
            dialog.close()

    def test_new_vm_secure_boot_defaults_on_for_x64_uefi(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        captured: dict = {}

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return [{"path": "/tmp/labs", "role": "labs"}]

            def networks_list(self) -> list[dict]:
                return [{"name": "default", "mode": "nat"}]

            def vm_create(self, **params) -> dict:
                captured.update(params)
                return {"name": params["name"]}

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.iso_edit.setText("/home/ISO/Win11_24H2_English_x64.iso")
            dialog._detect_iso()
            self.assertEqual(dialog.firmware_combo.currentData(), "efi")
            self.assertTrue(dialog.secure_boot_check.isChecked())
            self.assertTrue(dialog.secure_boot_check.isEnabled())
            dialog.name_edit.setText("win11-secure")
            dialog.dest_edit.setText("/tmp/labs")
            dialog._create()
            self.assertTrue(captured["secure_boot"])
        finally:
            dialog.close()

    def test_new_vm_secure_boot_opt_out_and_bios_gate(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        captured: dict = {}

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return [{"path": "/tmp/labs", "role": "labs"}]

            def networks_list(self) -> list[dict]:
                return [{"name": "default", "mode": "nat"}]

            def vm_create(self, **params) -> dict:
                captured.update(params)
                return {"name": params["name"]}

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.iso_edit.setText("/home/ISO/archlinux-x86_64.iso")
            dialog._detect_iso()
            # Linux defaults to UEFI and a 64 GB disk.
            self.assertEqual(dialog.firmware_combo.currentData(), "efi")
            self.assertEqual(dialog.disk_edit.text(), "64")
            dialog.firmware_combo.setCurrentIndex(
                dialog.firmware_combo.findData("bios")
            )
            self.assertFalse(dialog.secure_boot_check.isChecked())
            self.assertFalse(dialog.secure_boot_check.isEnabled())
            self.assertIn("Secure Boot: no", dialog.summary_label.text())
            dialog.firmware_combo.setCurrentIndex(
                dialog.firmware_combo.findData("efi")
            )
            # Linux defaults to UEFI without Secure Boot so unsigned media
            # still boots; the user can opt in.
            self.assertFalse(dialog.secure_boot_check.isChecked())
            self.assertTrue(dialog.secure_boot_check.isEnabled())
            dialog.secure_boot_check.setChecked(True)
            self.assertIn("Secure Boot: yes", dialog.summary_label.text())
            dialog.secure_boot_check.setChecked(False)
            self.assertFalse(dialog.secure_boot_check.isChecked())
            dialog.name_edit.setText("arch-lab")
            dialog.dest_edit.setText("/tmp/labs")
            dialog._create()
            self.assertFalse(captured["secure_boot"])

            dialog2 = NewVmDialog(FakeBackend())
            try:
                # Default variant is Windows Server 2025 (BIOS preset), so
                # Secure Boot stays disabled until UEFI is selected.
                self.assertEqual(
                    dialog2.firmware_combo.currentData(), "bios"
                )
                self.assertFalse(dialog2.secure_boot_check.isChecked())
                self.assertFalse(dialog2.secure_boot_check.isEnabled())
                dialog2.firmware_combo.setCurrentIndex(
                    dialog2.firmware_combo.findData("efi")
                )
                self.assertTrue(dialog2.secure_boot_check.isChecked())
                self.assertTrue(dialog2.secure_boot_check.isEnabled())
            finally:
                dialog2.close()
        finally:
            dialog.close()

    def test_new_vm_aarch64_disables_secure_boot(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.arch_combo.addItem("aarch64", "aarch64")
            dialog.arch_combo.setCurrentIndex(
                dialog.arch_combo.findData("aarch64")
            )
            dialog._arch_changed()
            self.assertEqual(dialog.firmware_combo.currentData(), "efi")
            self.assertFalse(dialog.secure_boot_check.isChecked())
            self.assertFalse(dialog.secure_boot_check.isEnabled())
        finally:
            dialog.close()

    def test_new_vm_warns_when_os_detection_is_overridden(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.iso_edit.setText("/home/ISO/Win11_24H2_English_x64.iso")
            dialog._detect_iso()
            dialog.family_combo.setCurrentIndex(
                dialog.family_combo.findData("linux")
            )
            self.assertEqual(dialog.detected_label.objectName(), "Warning")
            self.assertIn(
                "overrides the detected OS", dialog.detected_label.text()
            )
            self.assertEqual(dialog.disk_bus_combo.currentData(), "virtio")
            self.assertFalse(dialog.tpm_check.isChecked())
        finally:
            dialog.close()

    def test_vm_config_limits_cpu_for_v2_chain(self) -> None:
        from vemu.ui.vm_config import VmConfigDialog

        payload = {
            "state": "stopped",
            "pending_restart": False,
            "has_v2_chain": True,
            "config": {
                "name": "vm1",
                "uuid": "uuid",
                "memory_mib": 2048,
                "memory_max_mib": 2048,
                "vcpus": 2,
                "vcpus_max": 2,
                "cpu_mode": "host-passthrough",
                "machine": "q35",
                "arch": "x86_64",
                "firmware": "bios",
                "video": "virtio",
                "disks": [
                    {
                        "device": "disk",
                        "bus": "virtio",
                        "target": "vda",
                        "format": "qcow2",
                        "source": "/tmp/a.qcow2",
                    }
                ],
                "nics": [],
                "shares": [],
                "controllers": [],
                "other_devices": [],
            },
        }

        class FakeBackend:
            def vm_config_info(self, _name: str) -> dict:
                return payload

            def vm_capabilities(self, machine=None, arch="x86_64") -> dict:
                return {
                    "cpu_modes": [
                        "host-passthrough",
                        "host-model",
                        "custom",
                        "maximum",
                    ],
                    "disk_buses": ["virtio", "sata", "scsi", "nvme", "usb"],
                    "video_models": ["virtio"],
                    "nic_models": ["virtio"],
                }

        dialog = VmConfigDialog(FakeBackend(), "vm1")
        try:
            self.assertFalse(hasattr(dialog, "disk_bus_combo"))
            self.assertEqual(dialog.resize_disk_button.text(), "Expand disk…")
            cpu_values = [
                dialog.cpu_combo.itemData(i)
                for i in range(dialog.cpu_combo.count())
            ]
            self.assertEqual(
                cpu_values, ["host-passthrough", "host-model"]
            )
        finally:
            dialog.close()

    def test_vm_config_stages_added_disk_until_save(self) -> None:
        from PySide6.QtWidgets import QDialog

        from vemu.ui.vm_config import VmConfigDialog

        payload = {
            "state": "stopped",
            "pending_restart": False,
            "has_v2_chain": True,
            "config": {
                "name": "vm1",
                "uuid": "uuid",
                "memory_mib": 2048,
                "memory_max_mib": 2048,
                "vcpus": 2,
                "vcpus_max": 2,
                "cpu_mode": "host-passthrough",
                "machine": "q35",
                "arch": "x86_64",
                "firmware": "bios",
                "video": "virtio",
                "disks": [
                    {
                        "device": "disk",
                        "bus": "virtio",
                        "target": "vda",
                        "format": "qcow2",
                        "source": "/tmp/a.qcow2",
                    }
                ],
                "nics": [],
                "shares": [],
                "controllers": [],
                "other_devices": [],
            },
        }
        calls: list[dict] = []

        class FakeBackend:
            def vm_config_info(self, _name: str) -> dict:
                return payload

            def vm_capabilities(self, machine=None, arch="x86_64") -> dict:
                return {
                    "cpu_modes": ["host-passthrough", "host-model"],
                    "disk_buses": ["virtio", "sata"],
                    "video_models": ["virtio"],
                    "nic_models": ["virtio"],
                }

            def vm_disk_add(self, **kwargs):
                calls.append(kwargs)
                return {"disk": {"source": kwargs["path"]}}

        class FakeDialog:
            def __init__(self, *args, **kwargs) -> None:
                self.spec = {
                    "path": "/tmp/data-disk-1.qcow2",
                    "size_gb": 5,
                    "bus": "sata",
                    "format": "qcow2",
                    "create_file": True,
                }

            def exec(self):
                return QDialog.DialogCode.Accepted

            def close(self) -> None:
                return None

        dialog = VmConfigDialog(FakeBackend(), "vm1")
        try:
            with mock.patch(
                "vemu.ui.vm_config.DiskAddDialog", FakeDialog
            ):
                dialog._add_disk()
            self.assertEqual(len(dialog._pending_disks), 1)
            self.assertEqual(calls, [])
            self.assertEqual(dialog.disks.rowCount(), 2)
            self.assertIn("Pending disks", dialog.pending_label.text())
        finally:
            dialog.close()

    def test_vm_config_devices_table(self) -> None:
        from vemu.domain import Disk, DomainSpec
        from vemu.ui.vm_config import VmConfigDialog
        from vemu.vmconfig import parse_config

        spec = DomainSpec(
            name="vm1",
            memory_mib=1024,
            vcpus=2,
            machine="q35",
            disks=[Disk(path="/tmp/a.qcow2", bus="sata", target="sda")],
            guest_agent=True,
        )
        payload = {
            "config": parse_config(spec.to_xml()),
            "state": "stopped",
            "pending_restart": False,
        }

        class FakeBackend:
            def vm_config_info(self, name: str) -> dict:
                return payload

            def vm_capabilities(self, machine=None, arch="x86_64") -> dict:
                return {}

        dialog = VmConfigDialog(FakeBackend(), "vm1")
        try:
            types = [
                dialog.devices.item(row, 0).text()
                for row in range(dialog.devices.rowCount())
            ]
            self.assertIn("controller", types)
            self.assertIn("channel", types)
            self.assertIn("memballoon", types)
            self.assertIn("graphics", types)
            details = [
                dialog.devices.item(row, 1).text()
                for row in range(dialog.devices.rowCount())
            ]
            self.assertTrue(any("org.qemu.guest_agent.0" in item for item in details))
        finally:
            dialog.close()

    def test_main_window_disables_new_vm_without_labs(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def storage_registry(self) -> list[dict]:
                return []

            def vm_create(self, **_params) -> dict:
                return {}

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            window.refresh()
            self.assertFalse(window.new_vm_button.isEnabled())
            self.assertIn("labs storage", window.new_vm_button.toolTip())
        finally:
            window.close()

    def test_first_run_assets_and_compact_size(self) -> None:
        from vemu.ui.onboarding import FirstRunDialog

        class FakeBackend:
            def storage_registry(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        from PySide6.QtWidgets import QWidget

        from vemu.ui.theme import DARK

        owner = QWidget()
        owner.dark = True
        dialog = FirstRunDialog(FakeBackend(), owner)
        try:
            self.assertLessEqual(dialog.minimumHeight(), 360)
            self.assertEqual(dialog.progress.maximum(), 4)
            self.assertIn(DARK.accent_subtle, dialog.icon_label.styleSheet())
            for path in dialog._icons.values():
                self.assertTrue(path.is_file(), path)
        finally:
            dialog.close()

    def test_installer_wrapper_prefers_gtk(self) -> None:
        root = Path(__file__).resolve().parents[1]
        wrapper = (root / "packaging" / "vemu-installer.sh").read_text()
        installer = (
            root / "packaging" / "vemu-installer-gtk.py"
        ).read_text()
        self.assertTrue(
            (root / "packaging" / "vemu-installer-gtk.py").is_file()
        )
        self.assertIn("vemu-installer-gtk.py", wrapper)
        self.assertIn("VEMU_INSTALLER_NO_GTK", wrapper)
        self.assertIn("gtk-application-prefer-dark-theme", installer)
        self.assertIn("#181818", installer)
        self.assertIn("#2dd4bf", installer)
        self.assertNotIn("Requirements: a Linux desktop", installer)

    def test_first_run_button_logic_and_sizes(self) -> None:
        from vemu.ui.onboarding import FirstRunDialog

        class FakeBackend:
            def storage_registry(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        dialog = FirstRunDialog(FakeBackend())
        try:
            self.assertTrue(dialog.back_button.isHidden())
            self.assertFalse(dialog.skip_button.isHidden())
            widths = {
                dialog.back_button.width(),
                dialog.skip_button.width(),
                dialog.next_button.width(),
            }
            heights = {
                dialog.back_button.height(),
                dialog.skip_button.height(),
                dialog.next_button.height(),
            }
            self.assertEqual(len(widths), 1)
            self.assertEqual(len(heights), 1)

            dialog.step = 3
            dialog._show_step()
            self.assertFalse(dialog.back_button.isHidden())
            self.assertTrue(dialog.skip_button.isHidden())
            self.assertFalse(dialog.skip_spacer.isHidden())
            self.assertEqual(dialog.next_button.text(), "Finish")
        finally:
            dialog.close()

    def test_first_run_creates_labs_and_nat(self) -> None:
        from vemu.ui.onboarding import FirstRunDialog

        calls: list[tuple] = []

        class FakeBackend:
            def storage_registry(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

            def storage_add(self, path: str, role: str) -> dict:
                calls.append(("storage", path, role))
                return {"location": {"path": path, "role": role}}

            def network_add(self, *args) -> dict:
                calls.append(("network",) + args)
                return {"name": "lab-nat"}

            def network_apply(self) -> dict:
                calls.append(("apply",))
                return {"networks": []}

        dialog = FirstRunDialog(FakeBackend())
        try:
            self.assertEqual(dialog.step, 0)
            dialog._next()
            self.assertEqual(dialog.step, 1)
            dialog.storage_edit.setText("/home/tructt/vemu-labs")
            dialog._next()
            self.assertEqual(dialog.step, 2)
            dialog._next()
            self.assertEqual(dialog.step, 3)
            self.assertIn("/home/tructt/vemu-labs", dialog.summary_label.text())
            self.assertIn("lab-nat", dialog.summary_label.text())
            dialog._next()
            self.assertTrue(dialog.storage_done)
            self.assertTrue(dialog.network_done)
            self.assertIn(
                ("storage", "/home/tructt/vemu-labs", "labs"), calls
            )
            self.assertIn(("apply",), calls)
        finally:
            dialog.close()

    def test_first_run_skip_marks_dialog(self) -> None:
        from vemu.ui.onboarding import FirstRunDialog

        class FakeBackend:
            def storage_registry(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        dialog = FirstRunDialog(FakeBackend())
        try:
            dialog._skip()
            self.assertTrue(dialog.skipped)
            self.assertFalse(dialog.storage_done)
        finally:
            dialog.close()

    def test_first_run_recognizes_existing_labs_and_nat(self) -> None:
        from vemu.ui.onboarding import FirstRunDialog

        class FakeBackend:
            def storage_registry(self) -> list[dict]:
                return [{"path": "/srv/vemu/labs", "role": "labs"}]

            def networks_list(self) -> list[dict]:
                return [{"name": "lab-nat", "mode": "nat"}]

        dialog = FirstRunDialog(FakeBackend())
        try:
            self.assertTrue(dialog.storage_done)
            self.assertTrue(dialog.network_done)
            self.assertEqual(dialog.storage_edit.text(), "/srv/vemu/labs")
            self.assertIn("lab-nat", dialog.network_info.text())
        finally:
            dialog.close()

    def test_new_vm_wizard_navigation(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return [{"path": "/tmp/labs", "role": "labs"}]

            def networks_list(self) -> list[dict]:
                return [{"name": "lab-nat", "mode": "nat"}]

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.name_edit.setText("fedora-lab")
            dialog.iso_edit.setText("/home/ISO/Fedora-Workstation-44.iso")
            dialog._detect_iso()
            dialog.dest_edit.setText("/tmp/labs")
            self.assertEqual(dialog.step, 0)
            self.assertTrue(dialog.back_button.isHidden())
            self.assertFalse(dialog.next_button.isHidden())
            self.assertTrue(dialog.create_button.isHidden())

            dialog._next()
            self.assertEqual(dialog.step, 1)
            dialog._next()
            self.assertEqual(dialog.step, 2)
            dialog._next()
            self.assertEqual(dialog.step, 3)
            self.assertTrue(dialog.next_button.isHidden())
            self.assertFalse(dialog.create_button.isHidden())
            summary = dialog.summary_label.text()
            self.assertIn("fedora-lab", summary)
            self.assertIn("Fedora", summary)
            self.assertIn("Unattended install: no", summary)
            self.assertIn("/tmp/labs", summary)

            dialog._back()
            self.assertEqual(dialog.step, 2)
            self.assertEqual(dialog.name_edit.text(), "fedora-lab")
            self.assertEqual(dialog.variant_combo.currentData(), "fedora")
        finally:
            dialog.close()

    def test_new_vm_layout_stable_across_os_family(self) -> None:
        from vemu.ui.vm_create import NewVmDialog

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return []

            def networks_list(self) -> list[dict]:
                return []

        dialog = NewVmDialog(FakeBackend())
        try:
            dialog.show()
            self.app.processEvents()
            baseline = (
                dialog.name_edit.x(),
                dialog.name_edit.width(),
                dialog.helper_label.y(),
            )
            for family in ("linux", "other", "windows"):
                index = dialog.family_combo.findData(family)
                dialog.family_combo.setCurrentIndex(index)
                self.app.processEvents()
                current = (
                    dialog.name_edit.x(),
                    dialog.name_edit.width(),
                    dialog.helper_label.y(),
                )
                self.assertEqual(current, baseline)
        finally:
            dialog.close()

    def test_doctor_page(self) -> None:
        from vemu.ui.doctor_page import DoctorPage

        payload = {
            "ok": False,
            "checks": [
                {
                    "check": "qemu-img",
                    "ok": True,
                    "level": "ok",
                    "detail": "qemu-img version 10",
                },
                {
                    "check": "nft",
                    "ok": False,
                    "level": "fail",
                    "detail": "not found",
                },
            ],
        }

        class FakeBackend:
            def doctor(self) -> dict:
                return payload

        page = DoctorPage(FakeBackend())
        try:
            page.refresh()
            self.assertEqual(page.table.rowCount(), 2)
            self.assertEqual(page.table.item(0, 0).text(), "OK")
            self.assertEqual(page.table.item(1, 0).text(), "FAIL")
            self.assertIn("1 check(s) failed", page.summary.text())
            self.assertFalse(page.selinux_button.isEnabled())
        finally:
            page.close()

    def test_adopt_dialog_mode_choices(self) -> None:
        from vemu.ui.adopt import AdoptDialog

        class FakeBackend:
            def adopt_domain(self, _name, mode="partial"):
                return {"backup": "/tmp/backup.xml", "mode": mode}

        dialog = AdoptDialog(FakeBackend(), "vm1")
        try:
            self.assertTrue(dialog.read_only.isChecked())
            self.assertEqual(dialog.choice, "read-only")
            dialog.partial.setChecked(True)
            self.assertEqual(dialog.choice, "partial")
            self.assertIn("XML backup", dialog.description.text())
            dialog.full.setChecked(True)
            self.assertEqual(dialog.choice, "full")
            self.assertIn("v2 chain", dialog.description.text())
        finally:
            dialog.close()

    def test_doctor_page_selinux_repair_button(self) -> None:
        from vemu.ui.doctor_page import DoctorPage

        payload = {
            "ok": True,
            "checks": [
                {
                    "check": "SELinux",
                    "ok": True,
                    "level": "ok",
                    "detail": "Enforcing",
                },
            ],
        }

        class FakeBackend:
            def doctor(self) -> dict:
                return payload

            def storage_list(self) -> list:
                return [{"path": "/tmp/templates", "role": "templates"}]

            def storage_repair(self, *args, **kwargs) -> dict:
                return {"location": {}, "actions": []}

            def list_domains(self) -> list:
                return []

        page = DoctorPage(FakeBackend())
        try:
            page.refresh()
            self.assertTrue(page.selinux_button.isEnabled())
            self.assertEqual(page._selinux_detail(), "Enforcing")
        finally:
            page.close()

    def test_doctor_page_apparmor_hides_selinux_repair(self) -> None:
        from vemu.ui.doctor_page import DoctorPage

        payload = {
            "ok": True,
            "security": {
                "active": "apparmor",
                "selinux": {"mode": "Disabled", "active": False},
                "apparmor": {"active": True, "profiles": 17},
            },
            "checks": [
                {
                    "check": "AppArmor",
                    "ok": True,
                    "level": "ok",
                    "detail": "enabled (17 profiles)",
                },
            ],
        }

        class FakeBackend:
            def doctor(self) -> dict:
                return payload

            def storage_list(self) -> list:
                return [{"path": "/tmp/templates", "role": "templates"}]

            def storage_repair(self, *args, **kwargs) -> dict:
                return {"location": {}, "actions": []}

        page = DoctorPage(FakeBackend())
        try:
            page.refresh()
            self.assertTrue(page.selinux_button.isHidden())
            self.assertFalse(page.selinux_button.isEnabled())
            self.assertIn("AppArmor", page.security_label.text())
            self.assertIn("17", page.security_label.text())
        finally:
            page.close()

    def test_doctor_page_selinux_security_payload(self) -> None:
        from vemu.ui.doctor_page import DoctorPage

        payload = {
            "ok": True,
            "security": {
                "active": "selinux",
                "selinux": {"mode": "Enforcing", "active": True},
                "apparmor": {"active": False, "profiles": None},
            },
            "checks": [
                {
                    "check": "SELinux",
                    "ok": True,
                    "level": "ok",
                    "detail": "Enforcing",
                },
            ],
        }

        class FakeBackend:
            def doctor(self) -> dict:
                return payload

            def storage_list(self) -> list:
                return [{"path": "/tmp/templates", "role": "templates"}]

            def storage_repair(self, *args, **kwargs) -> dict:
                return {"location": {}, "actions": []}

        page = DoctorPage(FakeBackend())
        try:
            page.refresh()
            self.assertFalse(page.selinux_button.isHidden())
            self.assertTrue(page.selinux_button.isEnabled())
            self.assertIn("SELinux Enforcing", page.security_label.text())
        finally:
            page.close()

    def test_selinux_repair_task_forces_labels_and_reports_errors(self) -> None:
        from vemu.ui.doctor_page import _SelinuxRepairTask

        calls: list[tuple[str, str, bool]] = []

        class FakeBackend:
            def storage_list(self) -> list:
                return [
                    {"path": "/tmp/labs", "role": "labs"},
                    {"path": "/tmp/bad", "role": "isos"},
                ]

            def storage_repair(
                self, path: str, role: str, force_selinux: bool = False
            ) -> dict:
                calls.append((path, role, force_selinux))
                if path == "/tmp/bad":
                    raise RuntimeError("restorecon failed")
                return {
                    "location": {},
                    "actions": [{"applied": True}],
                }

        task = _SelinuxRepairTask(FakeBackend())
        output: list[tuple[object, object]] = []
        task.done.connect(
            lambda payload, error: output.append((payload, error))
        )
        task.run()
        self.assertTrue(output)
        payload, error = output[0]
        self.assertIsNone(error)
        self.assertEqual(
            calls,
            [
                ("/tmp/labs", "labs", True),
                ("/tmp/bad", "isos", True),
            ],
        )
        self.assertEqual(len(payload["locations"]), 1)
        self.assertEqual(payload["errors"][0]["path"], "/tmp/bad")
        self.assertIn("restorecon failed", payload["errors"][0]["error"])
        self.assertEqual(payload["actions"], 1)

    def test_labs_page(self) -> None:
        from vemu.ui.labs_page import LabsPage

        class FakeBackend:
            def lab_status(self, path: str) -> dict:
                return {
                    "lab": "demo",
                    "networks": [
                        {"name": "mgmt", "defined": True, "active": True},
                    ],
                    "vms": [
                        {
                            "name": "node1",
                            "state": "running",
                            "networks": ["mgmt"],
                            "template": "/tmp/template",
                        }
                    ],
                }

        page = LabsPage(FakeBackend())
        try:
            page.path_edit.setText("/tmp/lab.yaml")
            page.refresh()
            self.assertIn("demo", page.summary.text())
            self.assertEqual(page.networks.item(0, 0).text(), "mgmt")
            self.assertEqual(page.networks.item(0, 2).text(), "yes")
            self.assertEqual(page.vms.item(0, 0).text(), "node1")
            self.assertEqual(page.vms.item(0, 1).text(), "running")
            from PySide6.QtWidgets import QAbstractItemView

            self.assertEqual(
                page.vms.selectionMode(),
                QAbstractItemView.SelectionMode.ExtendedSelection,
            )
        finally:
            page.close()

    def test_lab_report_groups_duplicate_warnings(self) -> None:
        from vemu.ui.dialogs import ReportDialog
        from vemu.ui.labs_page import _group_lab_entries, _lab_known_names

        result = {
            "actions": [
                "network w11lab-nat: created",
                "win11-a: created from /t/win",
                "win11-b: created from /t/win",
                "alma-a: created from /t/alma",
            ],
            "warnings": [
                "win11-a: Clones receive a new TPM state.",
                "win11-a: Secure Boot is inherited from the template.",
                "win11-b: Clones receive a new TPM state.",
                "win11-b: Secure Boot is inherited from the template.",
                "alma-a: Clones receive a new TPM state.",
                "alma-a: Secure Boot is inherited from the template.",
            ],
        }
        known = _lab_known_names(result)
        changes = _group_lab_entries(result["actions"], known)
        warnings = _group_lab_entries(result["warnings"], known)
        self.assertEqual(
            changes,
            [
                "network w11lab-nat: created",
                "win11-a, win11-b: created from /t/win",
                "alma-a: created from /t/alma",
            ],
        )
        self.assertEqual(
            warnings,
            [
                "win11-a, win11-b, alma-a: Clones receive a new TPM state.",
                "win11-a, win11-b, alma-a: Secure Boot is inherited from "
                "the template.",
            ],
        )
        dialog = ReportDialog(
            None, "Lab up", changes=tuple(changes), warnings=tuple(warnings)
        )
        try:
            text = dialog.browser.toPlainText()
            self.assertEqual(text.count("Clones receive"), 1)
            self.assertEqual(text.count("Secure Boot is inherited"), 1)
            self.assertIn("win11-a, win11-b, alma-a", text)
        finally:
            dialog.close()

    def test_settings_cross_arch_disabled_without_qemu(self) -> None:
        from PySide6.QtCore import QSettings

        from vemu.errors import VemuError
        from vemu.ui.settings_page import SettingsPage

        class FakeBackend:
            def vm_capabilities(self, arch: str = "x86_64") -> dict:
                raise VemuError(f"no {arch} emulator")

        settings = QSettings("v-EMU", "v-EMU")
        settings.setValue("ui/cross_arch", True)
        page = SettingsPage(FakeBackend(), dark=True)
        try:
            self.assertFalse(page.cross_arch_check.isEnabled())
            self.assertFalse(page.cross_arch_check.isChecked())
            self.assertFalse(
                settings.value("ui/cross_arch", False, type=bool)
            )
            self.assertIn("not installed", page.cross_arch_note.text())
        finally:
            page.close()
            settings.setValue("ui/cross_arch", False)

    def test_settings_resource_profile(self) -> None:
        from vemu.ui.settings_page import SettingsPage

        captured: dict = {}

        class FakeBackend:
            def runtime_profile_get(self) -> dict:
                return {
                    "profile": "standard",
                    "description": "Prefer desktop and host work.",
                    "warnings": [],
                }

            def runtime_profile_set(self, profile: str) -> dict:
                captured["profile"] = profile
                return {
                    "profile": profile,
                    "description": "Prefer the VM over host work.",
                    "warnings": [],
                }

        page = SettingsPage(FakeBackend(), dark=True)
        try:
            self.assertTrue(page.profile_combo.isEnabled())
            self.assertEqual(
                page.profile_combo.currentData(), "standard"
            )
            self.assertIn("Best effort", page.profile_note.text())
            index = page.profile_combo.findData("ultra")
            page.profile_combo.setCurrentIndex(index)
            self.assertEqual(captured["profile"], "ultra")
            self.assertIn("Ultra", page.profile_combo.currentText())
        finally:
            page.close()

    def test_settings_cross_arch_enabled_with_qemu(self) -> None:
        from PySide6.QtCore import QSettings

        from vemu.ui.settings_page import SettingsPage

        class FakeBackend:
            def vm_capabilities(self, arch: str = "x86_64") -> dict:
                return {"machines": ["virt"]}

        settings = QSettings("v-EMU", "v-EMU")
        settings.setValue("ui/cross_arch", False)
        page = SettingsPage(FakeBackend(), dark=True)
        try:
            self.assertTrue(page.cross_arch_check.isEnabled())
            self.assertFalse(page.cross_arch_check.isChecked())
            self.assertIn("aarch64", page.cross_arch_note.text())
            self.assertIn("riscv64", page.cross_arch_note.text())
            self.assertIn("software emulation", page.cross_arch_warning.text())
            self.assertIn("heat", page.cross_arch_warning.text())
            page.cross_arch_check.setChecked(True)
            self.assertTrue(
                settings.value("ui/cross_arch", False, type=bool)
            )
        finally:
            page.close()
            settings.setValue("ui/cross_arch", False)

    def test_settings_page(self) -> None:
        from types import SimpleNamespace

        from vemu.ui.settings_page import SettingsPage

        class FakeBackend:
            client = SimpleNamespace(socket_path="/tmp/vemud.sock")

            def system_info(self) -> dict:
                return {
                    "version": "9.9.9",
                    "uri": "qemu:///system",
                }

        page = SettingsPage(FakeBackend(), dark=True)
        try:
            self.assertEqual(page.mode_label.text(), "vemud daemon")
            self.assertEqual(page.socket_label.text(), "/tmp/vemud.sock")
            self.assertEqual(page.version_label.text(), "9.9.9")
            self.assertEqual(page.scale_combo.count(), 4)
            self.assertEqual(page.scale_combo.currentData(), "1")
            changes: list[bool] = []
            page.theme_changed.connect(changes.append)
            page.theme_combo.setCurrentIndex(
                page.theme_combo.findData("light")
            )
            self.assertEqual(changes, [False])
            mini: list[bool] = []
            page.mini_console_changed.connect(mini.append)
            self.assertTrue(page.mini_console_check.isChecked())
            page.mini_console_check.setChecked(False)
            self.assertEqual(mini, [False])
            auto: list[tuple[bool, int]] = []
            page.auto_refresh_changed.connect(
                lambda enabled, interval: auto.append((enabled, interval))
            )
            page.set_auto_refresh(True, 30)
            page.auto_refresh_check.setChecked(False)
            self.assertFalse(page.auto_refresh_interval.isEnabled())
            self.assertEqual(auto[-1], (False, 30))
            page.auto_refresh_check.setChecked(True)
            page.auto_refresh_interval.setValue(45)
            self.assertEqual(auto[-1], (True, 45))
            page.scale_combo.setCurrentIndex(
                page.scale_combo.findData("1.25")
            )
            self.assertEqual(
                page._settings.value("ui/scale"), "1.25"
            )
            self.assertIn("Restart v-EMU", page.scale_note.text())
        finally:
            page.close()
            page._settings.setValue("ui/scale", "1")

    def test_theme_defaults_dark_and_remembers_choice(self) -> None:
        from PySide6.QtCore import QSettings

        from vemu.ui.app import _resolve_theme

        settings = QSettings("v-EMU", "v-EMU")
        previous = settings.value("ui/theme")
        try:
            settings.remove("ui/theme")
            self.assertTrue(_resolve_theme())
            settings.setValue("ui/theme", "light")
            self.assertFalse(_resolve_theme())
            settings.setValue("ui/theme", "dark")
            self.assertTrue(_resolve_theme())
            self.assertFalse(_resolve_theme(force_light=True))
            self.assertTrue(_resolve_theme(force_dark=True))
        finally:
            if previous is None:
                settings.remove("ui/theme")
            else:
                settings.setValue("ui/theme", previous)

    def test_ui_scale_setting_round_trip(self) -> None:
        from PySide6.QtCore import QSettings

        from vemu.ui.app import _resolve_ui_scale

        settings = QSettings("v-EMU", "v-EMU")
        previous = settings.value("ui/scale")
        try:
            settings.setValue("ui/scale", "1.5")
            with mock.patch.dict(
                os.environ, {"VEMU_UI_SCALE": ""}, clear=False
            ):
                self.assertEqual(_resolve_ui_scale(), "1.5")
            with mock.patch.dict(
                os.environ, {"VEMU_UI_SCALE": "1.25"}, clear=False
            ):
                self.assertEqual(_resolve_ui_scale(), "1.25")
            settings.setValue("ui/scale", "invalid")
            with mock.patch.dict(
                os.environ, {"VEMU_UI_SCALE": ""}, clear=False
            ):
                self.assertEqual(_resolve_ui_scale(), "1")
        finally:
            if previous is None:
                settings.remove("ui/scale")
            else:
                settings.setValue("ui/scale", previous)

    def test_network_map_dialog(self) -> None:
        from vemu.ui.network_map import NetworkMapDialog

        class FakeBackend:
            def network_topology(self) -> dict:
                return {
                    "host": {"name": "host1"},
                    "networks": [
                        {
                            "name": "lab-nat",
                            "mode": "nat",
                            "subnet": "10.213.0.0/24",
                            "bridge": "vemu-lab-nat",
                            "runtime": {"active": True},
                            "port_forwards": [
                                {
                                    "protocol": "tcp",
                                    "host_port": 2222,
                                    "guest_ip": "10.213.0.10",
                                    "guest_port": 22,
                                }
                            ],
                            "host_reservations": [],
                        }
                    ],
                    "vms": [
                        {
                            "name": "node1",
                            "state": "running",
                            "interfaces": [
                                {
                                    "network": "lab-nat",
                                    "mac": "52:54:00:aa:bb:cc",
                                    "ips": ["10.213.0.10"],
                                    "ip_source": "guest-agent",
                                }
                            ],
                        }
                    ],
                    "warnings": [],
                }

        dialog = NetworkMapDialog(FakeBackend(), dark=False)
        try:
            from PySide6.QtWidgets import QGraphicsEllipseItem

            self.assertGreater(len(dialog.scene.items()), 4)
            dots = [
                item
                for item in dialog.scene.items()
                if isinstance(item, QGraphicsEllipseItem)
            ]
            self.assertGreaterEqual(len(dots), 3)
            self.assertIn("lab-nat", dialog.payload["networks"][0]["name"])
            self.assertIsNotNone(dialog._icon("host"))
            self.assertIsNotNone(dialog._icon("switch"))
            self.assertIsNotNone(dialog._icon("guest"))
            dialog.view.zoom_in()
            self.assertGreater(dialog.view.zoom_percent, 100)
            self.assertEqual(dialog.zoom_label.text(), "115%")
            dialog.view.reset_zoom()
            self.assertEqual(dialog.view.zoom_percent, 100)
            dialog.view.fit()
            self.assertGreater(dialog.view.zoom_percent, 0)
        finally:
            dialog.close()

    def test_window_title_and_cli_console(self) -> None:
        from PySide6.QtCore import QSize

        from vemu.ui.app import MainWindow
        from vemu.ui.cli_console import CliConsoleDialog

        class FakeBackend:
            def list_domains(self) -> list:
                return []

            def storage_list(self) -> list:
                return []

            def networks_list(self) -> list:
                return []

            def network_topology(self) -> dict:
                return {"host": {"name": "h"}, "networks": [], "vms": []}

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            self.assertEqual(window.windowTitle(), "v-EMU")
            self.assertEqual(
                window.tagline_label.text(),
                "Desktop Virtualization for QEMU/KVM",
            )
            self.assertEqual(window.size().width(), 1280)
            self.assertEqual(window.size().height(), 720)
            self.assertEqual(window.minimumSize(), QSize(1280, 720))
            self.assertTrue(window.cli_button.isEnabled())
            console = CliConsoleDialog(FakeBackend(), window)
            try:
                self.assertIn("v-EMU console", console.output.toPlainText())
                console.input.setText("help")
                console._submit()
                self.assertIn(
                    "Common commands", console.output.toPlainText()
                )
                environment = console._process_environment()
                self.assertTrue(environment.value("PYTHONPATH"))
            finally:
                console.close()
        finally:
            window.close()

    def test_webengine_environment_flags(self) -> None:
        from vemu.ui.app import _configure_webengine_environment

        with mock.patch.dict(
            os.environ,
            {
                "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox",
                "VEMU_WEBENGINE_GPU": "",
            },
            clear=False,
        ):
            os.environ.pop("VEMU_WEBENGINE_GPU", None)
            _configure_webengine_environment()
            flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
            self.assertIn("--no-sandbox", flags)
            self.assertIn("--disable-gpu", flags)
            self.assertIn("--disable-gpu-compositing", flags)
            self.assertIn("--disable-accelerated-video-decode", flags)

        with mock.patch.dict(
            os.environ,
            {
                "QTWEBENGINE_CHROMIUM_FLAGS": "--use-gl=egl",
                "VEMU_WEBENGINE_GPU": "1",
            },
            clear=False,
        ):
            _configure_webengine_environment()
            self.assertEqual(
                os.environ["QTWEBENGINE_CHROMIUM_FLAGS"], "--use-gl=egl"
            )

    def test_seal_dialog(self) -> None:
        from vemu.ui.seal import SealDialog

        captured: dict = {}

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return [
                    {"path": "/tmp/labs", "role": "labs"},
                    {"path": "/tmp/templates", "role": "templates"},
                ]

            def seal(self, **params) -> dict:
                captured.update(params)
                return {"name": params["name"], "base": "/tmp/base.qcow2"}

        dialog = SealDialog(FakeBackend(), "win-lab")
        try:
            self.assertEqual(dialog.dest_edit.text(), "/tmp/templates")
            self.assertGreaterEqual(dialog.minimumHeight(), 480)
            self.assertGreaterEqual(dialog.name_edit.sizeHint().height(), 20)
            dialog.name_edit.setText("win2025-base")
            self.assertTrue(dialog.seal_button.isEnabled())
            self.assertFalse(dialog.bitlocker_check.isEnabled())
            index = dialog.os_family_combo.findData("windows")
            dialog.os_family_combo.setCurrentIndex(index)
            self.assertTrue(dialog.bitlocker_check.isEnabled())
            self.assertFalse(dialog.sysprep_warning.isHidden())
            self.assertFalse(dialog.seal_button.isEnabled())
            dialog.sysprepped_check.setChecked(True)
            self.assertTrue(dialog.sysprep_warning.isHidden())
            self.assertFalse(dialog.seal_button.isEnabled())
            dialog.bitlocker_check.setChecked(True)
            self.assertTrue(dialog.seal_button.isEnabled())
            dialog._seal()
            self.assertEqual(captured["name"], "win2025-base")
            self.assertEqual(captured["os_family"], "windows")
            self.assertTrue(captured["sysprepped"])
            self.assertNotIn("auto_sysprep", captured)
            self.assertTrue(captured["accept_bitlocker"])
            self.assertEqual(dialog.created["name"], "win2025-base")
        finally:
            dialog.close()

    def test_seal_dialog_windows_without_sysprep_is_allowed(self) -> None:
        from vemu.ui.seal import SealDialog

        captured: dict = {}

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return [{"path": "/tmp/templates", "role": "templates"}]

            def seal(self, **params) -> dict:
                captured.update(params)
                return {"name": params["name"], "base": "/tmp/base.qcow2"}

        dialog = SealDialog(FakeBackend(), "win-lab")
        try:
            index = dialog.os_family_combo.findData("windows")
            dialog.os_family_combo.setCurrentIndex(index)
            self.assertFalse(dialog.sysprep_warning.isHidden())
            dialog.bitlocker_check.setChecked(True)
            self.assertTrue(dialog.seal_button.isEnabled())
            dialog.name_edit.setText("win2025-unsysprepped")
            dialog._seal()
            self.assertFalse(captured["sysprepped"])
            self.assertNotIn("auto_sysprep", captured)
            self.assertTrue(captured["accept_bitlocker"])
        finally:
            dialog.close()

    def test_seal_dialog_linux_defaults(self) -> None:
        from vemu.ui.seal import SealDialog

        captured: dict = {}

        class FakeBackend:
            def storage_list(self) -> list[dict]:
                return [{"path": "/tmp/templates", "role": "templates"}]

            def seal(self, **params) -> dict:
                captured.update(params)
                return {"name": params["name"], "base": "/tmp/base.qcow2"}

        dialog = SealDialog(FakeBackend(), "rocky-lab")
        try:
            self.assertEqual(
                dialog.os_family_combo.currentData(), "linux"
            )
            self.assertTrue(dialog.windows_options.isHidden())
            self.assertTrue(dialog.seal_button.isEnabled())
            dialog.name_edit.setText("rocky-base")
            dialog._seal()
            self.assertEqual(captured["os_family"], "linux")
            self.assertFalse(captured["sysprepped"])
            self.assertFalse(captured["accept_bitlocker"])
        finally:
            dialog.close()

    def test_template_edit_and_clone_os_family(self) -> None:
        from vemu.ui.templates_page import (
            TemplateCloneDialog,
            TemplateEditDialog,
        )

        class FakeBackend:
            def clone_create(self, **params) -> dict:
                return {"created": [{"name": params["name"]}]}

        template = {
            "name": "rocky-cloud",
            "os_family": "linux",
            "sysprepped": False,
            "memory_mib": 2048,
            "vcpus": 2,
        }
        edit = TemplateEditDialog(None, template)
        try:
            self.assertEqual(edit.values(), ("linux", False))
            index = edit.os_family_combo.findData("windows")
            edit.os_family_combo.setCurrentIndex(index)
            edit.sysprepped_check.setChecked(True)
            self.assertEqual(edit.values(), ("windows", True))
        finally:
            edit.close()

        clone = TemplateCloneDialog(
            FakeBackend(),
            template,
            "/tmp/templates",
            "/tmp/labs",
            [{"name": "lab-nat", "mode": "nat"}],
        )
        try:
            from PySide6.QtWidgets import QScrollArea

            self.assertIsNotNone(clone.findChild(QScrollArea))
            self.assertLessEqual(clone.height(), 700)
            self.assertTrue(clone.cloud_init_check.isEnabled())
            self.assertFalse(clone.windows_oobe_check.isEnabled())
            clone.count_edit.setText("1")
            clone.name_edit.setText("clone-{{n}}")
            clone.dest_edit.setText("/tmp/labs")
            clone._clone()
        finally:
            clone.close()

    def test_template_import_dialog_os_family(self) -> None:
        from vemu.ui.templates_page import TemplateImportDialog

        dialog = TemplateImportDialog(None, "/tmp/templates")
        try:
            from PySide6.QtWidgets import QScrollArea

            self.assertIsNotNone(dialog.findChild(QScrollArea))
            self.assertLessEqual(dialog.height(), 640)
            self.assertEqual(
                dialog.os_family_combo.currentData(), "linux"
            )
            self.assertTrue(dialog.sysprepped_check.isHidden())
            index = dialog.os_family_combo.findData("windows")
            dialog.os_family_combo.setCurrentIndex(index)
            self.assertFalse(dialog.sysprepped_check.isHidden())
        finally:
            dialog.close()

    def test_templates_page_and_clone(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow
        from vemu.ui.templates_page import TemplateCloneDialog

        captured: dict = {}

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def storage_list(self) -> list[dict]:
                return [
                    {"path": "/tmp/labs", "role": "labs"},
                    {"path": "/tmp/templates", "role": "templates"},
                ]

            def networks_list(self) -> list[dict]:
                return [{"name": "lab-nat", "mode": "nat"}]

            def templates_list(self, root: str) -> list[dict]:
                return [
                    {
                        "name": "golden",
                        "arch": "x86_64",
                        "firmware": "bios",
                        "disk_bus": "sata",
                        "video": "vga",
                        "nic_model": "e1000e",
                        "sysprepped": True,
                        "memory_mib": 4096,
                        "vcpus": 2,
                        "base_size": 1024 ** 3,
                    }
                ]

            def clone_create(self, **params) -> dict:
                captured.update(params)
                return {"created": [{"name": params["name"]}]}

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
            try:
                page = window.templates_page
                page.root_manual = True
                page.root_edit.setText(tmp)
                page.root = tmp
                page.refresh()
                self.assertEqual(page.table.rowCount(), 1)
                self.assertEqual(page.table.item(0, 0).text(), "golden")
                page.table.selectRow(0)
                self.assertTrue(page.clone_button.isEnabled())

                template = page.templates[0]
                dialog = TemplateCloneDialog(
                    page.backend,
                    template,
                    tmp,
                    "/tmp/labs",
                    page._networks(),
                )
                try:
                    self.assertEqual(dialog.network_combo.count(), 1)
                    self.assertEqual(
                        dialog.network_combo.currentData(), "lab-nat"
                    )
                    self.assertFalse(dialog.windows_oobe_check.isChecked())
                    self.assertFalse(
                        dialog.windows_password_edit.isEnabled()
                    )
                    dialog.windows_oobe_check.setChecked(True)
                    self.assertTrue(
                        dialog.windows_password_edit.isEnabled()
                    )
                    dialog.windows_oobe_check.setChecked(False)
                    self.assertFalse(
                        dialog.windows_password_edit.isEnabled()
                    )
                    dialog.count_edit.setText("2")
                    dialog.name_edit.setText("clone-{{n}}")
                    dialog.dest_edit.setText("/tmp/labs")
                    dialog.cloud_init_check.setChecked(False)
                    dialog._clone()
                    self.assertEqual(captured["count"], 2)
                    self.assertEqual(captured["name"], "clone-{{n}}")
                    self.assertTrue(captured["define"])
                    self.assertTrue(captured["start"])
                    self.assertFalse(captured["cloud_init"])
                    self.assertFalse(captured["reset_identity"])
                    self.assertFalse(captured["windows_oobe"])
                    self.assertIsNone(captured["windows_admin_password"])
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_templates_page_uses_registered_root(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def __init__(self, root: str) -> None:
                self.root = root

            def list_domains(self) -> list[DomainInfo]:
                return []

            def storage_registry(self) -> list[dict]:
                return [{"path": self.root, "role": "templates"}]

            def storage_list(self) -> list[dict]:
                return [
                    {"path": self.root, "role": "templates"},
                    {"path": "/tmp/labs", "role": "labs"},
                ]

            def templates_list(self, _root: str) -> list[dict]:
                return []

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            templates = Path(tmp) / "templates"
            templates.mkdir()
            window = MainWindow(FakeBackend(str(templates)), dark=False)  # type: ignore[arg-type]
            try:
                page = window.templates_page
                self.assertEqual(page.root_edit.text(), str(templates))
                self.assertFalse(page.root_manual)
                page.refresh()
                self.assertEqual(page.root_edit.text(), str(templates))
            finally:
                window.close()

    def test_guide_page(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            button = next(
                item for item in window._nav_buttons if item.text() == "Guide"
            )
            button.click()
            self.assertIs(window.stack.currentWidget(), window.guide_page)
            text = window.guide_page.browser.toPlainText()
            self.assertIn("First use", text)
            self.assertIn("Lab-first workflows", text)
            self.assertIn("cloud-init", text)
            self.assertIn("Keyboard shortcuts", text)
            self.assertIn("Ctrl+Shift+M", text)
            self.assertIn("Ctrl+Shift+K", text)
            self.assertIn("Ctrl+Alt+Insert", text)
            self.assertIn("Driver ISO", text)
            self.assertNotIn("Detailed references", text)
            self.assertNotIn("./setup.sh", text)
            from PySide6.QtCore import Qt
            from PySide6.QtWidgets import QLabel

            browser = window.guide_page.browser
            self.assertEqual(
                browser.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
            self.assertTrue(
                any(
                    "Ctrl + mouse wheel" in label.text()
                    for label in window.guide_page.findChildren(QLabel)
                )
            )
            browser.zoomIn(4)
            self.assertFalse(browser.horizontalScrollBar().isVisible())
            browser.zoomOut(4)
            self.assertFalse(browser.horizontalScrollBar().isVisible())
            from vemu.ui.markdown import iter_links

            self.assertEqual(list(iter_links(window.guide_page._markdown)), [])
            self.assertNotIn("http://", window.guide_page._markdown)
            self.assertNotIn("https://", window.guide_page._markdown)
        finally:
            window.close()

    def test_vm_config_dialog(self) -> None:
        from vemu.ui.vm_config import VmConfigDialog

        captured: dict = {}

        class FakeBackend:
            def vm_config_info(self, domain: str) -> dict:
                return {
                    "state": "stopped",
                    "pending_restart": False,
                    "config": {
                        "name": domain,
                        "uuid": "uuid-test",
                        "memory_mib": 2048,
                        "vcpus": 2,
                        "cpu_mode": "host-passthrough",
                        "machine": "q35",
                        "firmware": "bios",
                        "video": "virtio",
                        "disks": [
                            {
                                "device": "disk",
                                "bus": "virtio",
                                "target": "vda",
                                "format": "qcow2",
                                "source": "/tmp/disk.qcow2",
                            }
                        ],
                        "nics": [
                            {
                                "mac": "52:54:00:aa:bb:cc",
                                "network": "default",
                                "model": "virtio",
                            }
                        ],
                    },
                }

            def vm_config_update(self, **params) -> dict:
                captured.update(params)
                return {
                    "changes": ["memory_mib: 2048 → 4096"],
                    "pending_restart": False,
                    "config": {},
                }

            def vm_serial_add(self, domain: str) -> dict:
                captured["serial_domain"] = domain
                return {"domain": domain, "serial": True, "changed": True}

        dialog = VmConfigDialog(FakeBackend(), "vm1")
        try:
            self.assertEqual(
                [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())],
                ["Core", "Storage", "Network", "Devices"],
            )
            self.assertIn("uuid-test", dialog.uuid_label.text())
            self.assertIn("viogpudo", dialog.video_badge.toolTip())
            dialog.memory_spin.setValue(4.0)
            dialog.video_combo.setCurrentIndex(
                dialog.video_combo.findData("qxl")
            )
            self.assertIn("QXL 128", dialog.video_badge.toolTip())
            dialog._save()
            self.assertEqual(captured["memory_mib"], 4096)
            self.assertEqual(captured["video"], "qxl")
            self.assertEqual(captured["video_memory_mib"], 64)
            self.assertTrue(dialog.video_memory_combo.isEnabled())
            self.assertTrue(dialog.serial_button.isEnabled())
            with mock.patch(
                "vemu.ui.vm_config.QMessageBox.information"
            ):
                dialog._add_serial()
            self.assertEqual(captured["serial_domain"], "vm1")
            self.assertEqual(dialog.disks.rowCount(), 1)
            self.assertEqual(dialog.nics.rowCount(), 1)
            self.assertEqual(dialog.nics.columnCount(), 4)
            self.assertEqual(dialog.nics.item(0, 3).text(), "Up")
            dialog.nics.selectRow(0)
            self.assertTrue(dialog.nic_link_button.isEnabled())
            self.assertTrue(dialog.change_network_button.isEnabled())
            self.assertEqual(dialog.nic_link_button.text(), "Disconnect")
        finally:
            dialog.close()

    def test_empty_states(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class EmptyBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def storage_list(self) -> list[dict]:
                return []

            def close(self) -> None:
                return None

        window = MainWindow(EmptyBackend(), dark=False)  # type: ignore[arg-type]
        try:
            self.assertFalse(window.table._empty.isHidden())
            self.assertEqual(
                window.table._empty_title.text(), "No virtual machines yet"
            )
            window.storage_page.refresh()
            self.assertFalse(window.storage_page.table._empty.isHidden())
            self.assertEqual(
                window.storage_page.table._empty_title.text(),
                "No storage locations yet",
            )
        finally:
            window.close()

        class NoStorageBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def close(self) -> None:
                return None

        window = MainWindow(NoStorageBackend(), dark=False)  # type: ignore[arg-type]
        try:
            window.storage_page.refresh()
            self.assertEqual(
                window.storage_page.table._empty_title.text(),
                "Storage needs vemud",
            )
        finally:
            window.close()

    def test_search_empty_state(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return [DomainInfo("alpha", "uuid-1", "stopped", [])]

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            window.search.setText("zzz")
            self.assertFalse(window.table._empty.isHidden())
            self.assertEqual(
                window.table._empty_title.text(),
                "No matching virtual machines",
            )
            window.search.clear()
            self.assertTrue(window.table._empty.isHidden())
        finally:
            window.close()

    def test_storage_context_menu(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def storage_list(self) -> list[dict]:
                return [
                    {
                        "path": "/tmp/labs",
                        "role": "labs",
                        "filesystem": "btrfs",
                        "free_bytes": 40 * 1024 ** 3,
                        "selinux": {
                            "mode": "Enforcing",
                            "label": "virt_image_t",
                            "expected": "virt_image_t",
                        },
                        "qemu_access": {
                            "traverse": True,
                            "read": True,
                            "write": True,
                        },
                        "nodatacow": True,
                        "owner": "qemu:qemu",
                        "mode": "750",
                        "issues": [],
                        "healthy": True,
                    }
                ]

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            page = window.storage_page
            page.refresh()
            menu = page._build_context_menu(page.locations[0])
            actions = {
                action.text(): action
                for action in menu.actions()
                if not action.isSeparator()
            }
            self.assertIn("Add location…", actions)
            self.assertIn("Fix labels and access", actions)
            self.assertIn("Remove from registry", actions)
            self.assertIn("Copy path", actions)
            self.assertTrue(actions["Fix labels and access"].isEnabled())

            empty = page._build_context_menu(None)
            empty_actions = {
                action.text(): action
                for action in empty.actions()
                if not action.isSeparator()
            }
            self.assertFalse(empty_actions["Fix labels and access"].isEnabled())
            self.assertFalse(empty_actions["Copy path"].isEnabled())
        finally:
            window.close()

    def test_snapshot_context_menu(self) -> None:
        from vemu.ui.snapshots import SnapshotsDialog

        class FakeBackend:
            def snapshots_info(self, name: str) -> dict:
                return {
                    "managed": True,
                    "running": False,
                    "chain": "base",
                    "active": "0001",
                    "layers": [
                        {
                            "id": "0001",
                            "label": "base",
                            "parent": "",
                            "actual_size": 20 * 1024 * 1024,
                            "active": True,
                        },
                        {
                            "id": "0002",
                            "label": "snap",
                            "parent": "0001",
                            "actual_size": 8 * 1024 * 1024,
                            "active": False,
                        },
                    ],
                }

        dialog = SnapshotsDialog(FakeBackend(), "vm1")
        try:
            dialog.table.setCurrentItem(
                dialog.table.topLevelItem(0).child(0)
            )
            menu = dialog._build_context_menu()
            actions = {
                action.text(): action
                for action in menu.actions()
                if not action.isSeparator()
            }
            self.assertIn("New snapshot", actions)
            self.assertIn("Revert", actions)
            self.assertIn("Delete", actions)
            self.assertIn("Copy ID", actions)
            self.assertTrue(actions["Revert"].isEnabled())
            self.assertTrue(actions["Delete"].isEnabled())
        finally:
            dialog.close()

    def test_network_page(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def networks_list(self) -> list[dict]:
                return [
                    {
                        "name": "labnet",
                        "mode": "nat",
                        "subnet": "10.213.0.0/24",
                        "dhcp": True,
                        "dns_domain": "vemu.local",
                        "description": "",
                        "libvirt_name": "vemu-labnet",
                        "bridge": "vbr-12345678",
                        "gateway": "10.213.0.1",
                        "has_host_ip": True,
                        "host_reservations": [],
                        "port_forwards": [
                            {
                                "protocol": "tcp",
                                "host_port": 2222,
                                "guest_ip": "10.213.0.10",
                                "guest_port": 22,
                                "description": "ssh",
                            }
                        ],
                        "runtime": {
                            "defined": True,
                            "active": True,
                            "autostart": True,
                            "bridge": "vbr-12345678",
                            "subnet": "10.213.0.0/24",
                        },
                    }
                ]

            def network_apply(self) -> dict:
                return {
                    "networks": [{"name": "labnet", "action": "updated"}],
                    "nft": {"applied": False, "reason": "not root"},
                }

            def network_remove(self, name: str, purge: bool = False) -> dict:
                return {"removed": {"name": name}, "purged": purge}

            def network_port_forward_remove(
                self, name: str, protocol: str, host_port: int
            ) -> dict:
                return {"profile": {"name": name, "port_forwards": []}}

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            button = next(
                item for item in window._nav_buttons if item.text() == "Networks"
            )
            button.click()
            self.assertIs(window.stack.currentWidget(), window.network_page)
            page = window.network_page
            self.assertEqual(page.table.rowCount(), 1)
            self.assertEqual(page.table.item(0, 0).text(), "labnet")
            self.assertEqual(page.table.item(0, 2).text(), "10.213.0.0/24")
            page.table.selectRow(0)
            self.assertEqual(page.forwards.rowCount(), 1)
            self.assertEqual(page.forwards.item(0, 1).text(), "2222")
            self.assertIn("labnet", page.details.text())

            menu = page._build_context_menu(page.profiles[0])
            actions = {
                action.text(): action
                for action in menu.actions()
                if not action.isSeparator()
            }
            self.assertIn("Apply all networks", actions)
            self.assertIn("Add port forward…", actions)
            self.assertIn("Remove network…", actions)
            self.assertTrue(actions["Add port forward…"].isEnabled())
            self.assertIn("DHCP reservations…", actions)
            self.assertTrue(page.reservations_button.isEnabled())
        finally:
            window.close()

    def test_network_empty_state(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class EmptyBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def networks_list(self) -> list[dict]:
                return []

            def close(self) -> None:
                return None

        window = MainWindow(EmptyBackend(), dark=False)  # type: ignore[arg-type]
        try:
            window.network_page.refresh()
            self.assertFalse(window.network_page.table._empty.isHidden())
            self.assertEqual(
                window.network_page.table._empty_title.text(),
                "No networks yet",
            )
        finally:
            window.close()

        class DirectBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def close(self) -> None:
                return None

        window = MainWindow(DirectBackend(), dark=False)  # type: ignore[arg-type]
        try:
            window.network_page.refresh()
            self.assertEqual(
                window.network_page.table._empty_title.text(),
                "Networks need vemud",
            )
        finally:
            window.close()

    def test_vm_network_dialog(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from vemu.ui.network_page import VmNetworkDialog

        calls: list[tuple[str, str, str]] = []

        class FakeBackend:
            def domain_interfaces(self, domain: str) -> list[dict]:
                return [
                    {
                        "mac": "52:54:00:aa:bb:cc",
                        "type": "network",
                        "network": "lab-nat",
                        "bridge": None,
                        "model": "virtio",
                        "target": "vnet0",
                    }
                ]

            def networks_list(self) -> list[dict]:
                return [{"name": "lab-nat"}, {"name": "lab-iso"}]

            def network_attach(self, domain: str, network: str) -> dict:
                calls.append(("attach", domain, network))
                return {}

            def network_detach(self, domain: str, mac: str) -> dict:
                calls.append(("detach", domain, mac))
                return {}

        dialog = VmNetworkDialog(FakeBackend(), "router-1")
        try:
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(dialog.table.item(0, 1).text(), "lab-nat")
            dialog.table.selectRow(0)
            self.assertTrue(dialog.detach_button.isEnabled())

            with mock.patch(
                "vemu.ui.network_page.QInputDialog.getItem",
                return_value=("lab-iso", True),
            ):
                dialog._attach()
            self.assertEqual(calls[-1], ("attach", "router-1", "lab-iso"))

            dialog.table.selectRow(0)
            with mock.patch(
                "vemu.ui.network_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog._detach()
            self.assertEqual(
                calls[-1], ("detach", "router-1", "52:54:00:aa:bb:cc")
            )
        finally:
            dialog.close()

    def test_storage_page(self) -> None:
        from vemu.backend import DomainInfo
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self) -> list[DomainInfo]:
                return []

            def storage_list(self) -> list[dict]:
                return [
                    {
                        "path": "/tmp/labs",
                        "role": "labs",
                        "filesystem": "btrfs",
                        "free_bytes": 40 * 1024 ** 3,
                        "selinux": {
                            "mode": "Enforcing",
                            "label": "virt_image_t",
                            "expected": "virt_image_t",
                        },
                        "qemu_access": {
                            "traverse": True,
                            "read": True,
                            "write": True,
                        },
                        "nodatacow": True,
                        "owner": "qemu:qemu",
                        "mode": "750",
                        "issues": [],
                        "healthy": True,
                    }
                ]

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)  # type: ignore[arg-type]
        try:
            storage_button = next(
                button
                for button in window._nav_buttons
                if button.text() == "Storage"
            )
            storage_button.click()
            self.assertIs(window.stack.currentWidget(), window.storage_page)
            self.assertEqual(window.storage_page.table.rowCount(), 1)
            self.assertEqual(window.storage_page.table.item(0, 0).text(), "/tmp/labs")
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
