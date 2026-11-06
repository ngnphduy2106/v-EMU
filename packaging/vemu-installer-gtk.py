#!/usr/bin/env python3
"""Single-window GTK installer for v-EMU.

This is the preferred `.run` UI on desktops with PyGObject/GTK3. It only
orchestrates setup.sh; the shell installer remains the fallback and owns the
actual host changes.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
except Exception as exc:  # pragma: no cover - fallback path
    print(f"GTK3 is unavailable: {exc}", file=sys.stderr)
    raise SystemExit(2)


PAGES = ("Welcome", "License", "Precheck", "Setup", "Finish")
HEADER_TITLES = (
    "Install v-EMU",
    "License",
    "Precheck",
    "Installing",
    "Done",
)


class InstallerWindow(Gtk.Window):
    def __init__(self, root: Path, mode: str, assume: bool = False) -> None:
        super().__init__(title="v-EMU Installer")
        self.root = root
        self.mode = mode
        self.assume = assume
        self.page = 0
        self.precheck_ok = False
        self.installed = False
        self.busy = False
        self._pulse_id: int | None = None
        self._keepalive = False
        self._follow_log = True
        self._auto_scrolling = False
        self._scroll_pending = False

        self.set_default_size(680, 450)
        self.set_size_request(620, 410)
        self.set_border_width(0)
        self.set_position(Gtk.WindowPosition.CENTER)
        titlebar = Gtk.HeaderBar()
        titlebar.set_show_close_button(True)
        titlebar.set_title("v-EMU")
        self.set_titlebar(titlebar)
        try:
            self.set_icon_from_file(
                str(
                    self.root
                    / "src/vemu/ui/assets/org.vemu.VEMU.svg"
                )
            )
        except Exception:
            pass
        self.connect("delete-event", self._on_close)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        self._apply_css()

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        header.set_border_width(18)
        header.get_style_context().add_class("installer-header")
        outer.pack_start(header, False, False, 0)

        frame = Gtk.EventBox()
        frame.set_size_request(56, 56)
        frame.set_valign(Gtk.Align.CENTER)
        frame.get_style_context().add_class("icon-frame")
        header.pack_start(frame, False, False, 0)
        icon_align = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        icon_align.set_halign(Gtk.Align.CENTER)
        icon_align.set_valign(Gtk.Align.CENTER)
        frame.add(icon_align)
        self.icon = Gtk.Image()
        icon_align.add(self.icon)

        self.title = Gtk.Label(label=HEADER_TITLES[0])
        self.title.set_xalign(0)
        self.title.set_valign(Gtk.Align.CENTER)
        self.title.get_style_context().add_class("step-title")
        header.pack_start(self.title, True, True, 0)

        self.progress = Gtk.ProgressBar()
        self.progress.set_hexpand(True)
        self.progress.set_margin_start(18)
        self.progress.set_margin_end(18)
        outer.pack_start(self.progress, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(160)
        self.stack.set_border_width(18)
        outer.pack_start(self.stack, True, True, 0)

        self._build_welcome()
        self._build_license()
        self._build_precheck()
        self._build_setup()
        self._build_finish()

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_border_width(12)
        outer.pack_start(buttons, False, False, 0)
        self.back_button = Gtk.Button(label="Back")
        self.back_button.connect("clicked", self._back)
        buttons.pack_start(self.back_button, False, False, 0)
        buttons.pack_start(Gtk.Box(), True, True, 0)

        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", lambda _button: self._cancel())
        buttons.pack_start(self.cancel_button, False, False, 0)

        self.next_button = Gtk.Button(label="Continue")
        self.next_button.get_style_context().add_class("suggested-action")
        self.next_button.connect("clicked", self._next)
        buttons.pack_start(self.next_button, False, False, 0)

        self.reboot_later_button = Gtk.Button(label="Reboot later")
        self.reboot_later_button.connect(
            "clicked", lambda _button: self.close()
        )
        buttons.pack_start(self.reboot_later_button, False, False, 0)
        self.reboot_now_button = Gtk.Button(label="Reboot now")
        self.reboot_now_button.get_style_context().add_class(
            "suggested-action"
        )
        self.reboot_now_button.connect("clicked", self._reboot)
        buttons.pack_start(self.reboot_now_button, False, False, 0)

        action_buttons = (
            self.back_button,
            self.cancel_button,
            self.next_button,
            self.reboot_later_button,
            self.reboot_now_button,
        )
        button_width = max(
            button.get_preferred_width()[1] for button in action_buttons
        )
        button_width = max(button_width, 112)
        for button in action_buttons:
            button.set_size_request(button_width, 34)

        self._set_icon(0)
        self._show_page(0)

    # ------------------------------------------------------------------ pages

    def _apply_css(self) -> None:
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", True)
        screen = Gdk.Screen.get_default()
        if screen is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"window, .background {"
            b" background-color: #181818;"
            b" color: #f0f0f0;"
            b" }"
            b"headerbar {"
            b" background-image: none;"
            b" background-color: #181818;"
            b" color: #f0f0f0;"
            b" border-bottom: 1px solid #2c2c2c;"
            b" box-shadow: none;"
            b" }"
            b".installer-header {"
            b" background-color: #181818;"
            b" }"
            b".icon-frame {"
            b" background-color: #16302c;"
            b" border: 1px solid #2c2c2c;"
            b" border-radius: 14px;"
            b" }"
            b".step-title {"
            b" font-size: 18px;"
            b" font-weight: 700;"
            b" color: #f0f0f0;"
            b" }"
            b"label {"
            b" color: #f0f0f0;"
            b" }"
            b".dim-label {"
            b" color: #a0a0a0;"
            b" }"
            b".body {"
            b" font-size: 13px;"
            b" }"
            b"textview, textview text, text {"
            b" background-color: #1e1e1e;"
            b" color: #f0f0f0;"
            b" }"
            b"scrolledwindow {"
            b" border: 1px solid #2c2c2c;"
            b" border-radius: 10px;"
            b" }"
            b"button {"
            b" background-image: none;"
            b" background-color: #1e1e1e;"
            b" color: #f0f0f0;"
            b" border: 1px solid #4c4c4c;"
            b" border-radius: 6px;"
            b" padding: 6px 14px;"
            b" }"
            b"button:hover {"
            b" background-color: #242424;"
            b" }"
            b"button:disabled {"
            b" color: #6b6b6b;"
            b" border-color: #2c2c2c;"
            b" }"
            b"button.suggested-action {"
            b" background-color: #0f766e;"
            b" color: #ffffff;"
            b" border-color: #0f766e;"
            b" font-weight: 600;"
            b" }"
            b"button.suggested-action:hover {"
            b" background-color: #115e59;"
            b" border-color: #115e59;"
            b" }"
            b"button.destructive-action {"
            b" color: #f87171;"
            b" }"
            b"checkbutton {"
            b" color: #f0f0f0;"
            b" }"
            b"progressbar {"
            b" min-height: 4px;"
            b" }"
            b"progressbar trough {"
            b" background-color: #2c2c2c;"
            b" border-radius: 2px;"
            b" }"
            b"progressbar progress {"
            b" background-color: #2dd4bf;"
            b" border-radius: 2px;"
            b" }"
        )
        Gtk.StyleContext.add_provider_for_screen(
            screen,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _page(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.stack.add_named(box, PAGES[self.page])
        return box

    @staticmethod
    def _text_view() -> Gtk.TextView:
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_left_margin(8)
        view.set_right_margin(8)
        view.set_top_margin(6)
        view.set_bottom_margin(6)
        return view

    @staticmethod
    def _wrap(label: Gtk.Label) -> Gtk.Label:
        label.set_line_wrap(True)
        label.set_xalign(0)
        return label

    def _build_welcome(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.stack.add_named(box, PAGES[0])
        body = self._wrap(Gtk.Label(label="Install v-EMU on this computer."))
        body.get_style_context().add_class("body")
        box.pack_start(body, False, False, 0)
        for text in (
            "QEMU/KVM lab virtualization.",
            "Requires Linux, KVM and sudo for host changes.",
        ):
            label = self._wrap(Gtk.Label(label=text))
            label.get_style_context().add_class("dim-label")
            box.pack_start(label, False, False, 0)
        box.pack_start(Gtk.Box(), True, True, 0)

    def _build_license(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.stack.add_named(box, PAGES[1])
        note = self._wrap(Gtk.Label(label="GNU GPL v3.0 or later."))
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)
        view = self._text_view()
        license_path = self.root / "LICENSE"
        try:
            view.get_buffer().set_text(
                license_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            view.get_buffer().set_text("LICENSE file was not found.")
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.add(view)
        box.pack_start(scroll, True, True, 0)
        self.license_check = Gtk.CheckButton(
            label="I accept the GNU General Public License v3.0 or later"
        )
        self.license_check.set_active(self.assume)
        self.license_check.connect("toggled", lambda _button: self._sync())
        box.pack_start(self.license_check, False, False, 0)

    def _build_precheck(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.stack.add_named(box, PAGES[2])
        self.precheck_label = self._wrap(Gtk.Label(
            label="Checking KVM and host tools…"
        ))
        box.pack_start(self.precheck_label, False, False, 0)
        self.precheck_view = self._text_view()
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.precheck_view)
        box.pack_start(scroll, True, True, 0)

    def _build_setup(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.stack.add_named(box, PAGES[3])
        self.setup_label = self._wrap(Gtk.Label(
            label="Installing. This can take several minutes."
        ))
        box.pack_start(self.setup_label, False, False, 0)
        self.setup_view = self._text_view()
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.setup_view)
        self.setup_view.get_vadjustment().connect(
            "value-changed", self._on_log_scrolled
        )
        box.pack_start(scroll, True, True, 0)

    def _build_finish(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.stack.add_named(box, PAGES[4])
        self.finish_label = self._wrap(Gtk.Label(label=""))
        self.finish_label.get_style_context().add_class("body")
        box.pack_start(self.finish_label, False, False, 0)
        note = self._wrap(Gtk.Label(label="Reboot recommended before first use."))
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)
        box.pack_start(Gtk.Box(), True, True, 0)

    # ------------------------------------------------------------------ state

    def _set_icon(self, page: int) -> None:
        names = (
            "onboarding-welcome.svg",
            "onboarding-license.svg",
            "onboarding-precheck.svg",
            "onboarding-setup.svg",
            "onboarding-ready.svg",
        )
        path = self.root / "src/vemu/ui/assets" / names[page]
        try:
            self.icon.set_from_file(str(path))
            self.icon.set_pixel_size(40)
        except Exception:
            self.icon.set_from_icon_name("system-run", Gtk.IconSize.DIALOG)

    def _sync(self) -> None:
        self.back_button.set_visible(self.page not in (0, 4))
        self.back_button.set_sensitive(not self.busy and self.page not in (0, 4))
        self.cancel_button.set_visible(self.page != 4)
        self.cancel_button.set_sensitive(not self.busy)
        self.next_button.set_visible(self.page != 4)
        self.reboot_later_button.set_visible(self.page == 4)
        self.reboot_now_button.set_visible(self.page == 4)
        self.reboot_now_button.set_sensitive(not self.busy)
        self.next_button.set_sensitive(not self.busy)
        accent = True
        if self.page == 1:
            self.next_button.set_label("Continue")
            accent = self.license_check.get_active() and not self.busy
            self.next_button.set_sensitive(accent)
        elif self.page == 2:
            self.next_button.set_label("Continue")
            accent = self.precheck_ok and not self.busy
            self.next_button.set_sensitive(accent)
        elif self.page == 3:
            self.next_button.set_label(
                "Install" if not self.installed else "Finish"
            )
            accent = not self.busy
        elif self.page == 0:
            self.next_button.set_label("Continue")
        self._set_next_accent(accent)

    def _set_next_accent(self, enabled: bool) -> None:
        context = self.next_button.get_style_context()
        if enabled:
            if not context.has_class("suggested-action"):
                context.add_class("suggested-action")
        elif context.has_class("suggested-action"):
            context.remove_class("suggested-action")

    def _set_progress(self) -> None:
        self.progress.set_fraction((self.page + 1) / len(PAGES))

    def _show_page(self, page: int) -> None:
        self.page = page
        self.stack.set_visible_child_name(PAGES[page])
        self.title.set_text(HEADER_TITLES[page])
        self._set_progress()
        self._set_icon(page)
        self._sync()

    # ------------------------------------------------------------------ actions

    def _back(self, _button=None) -> None:
        if self.busy or self.page <= 0 or self.page >= 4:
            return
        self._show_page(self.page - 1)

    def _cancel(self) -> None:
        if self.busy:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Cancel v-EMU installation?",
        )
        dialog.set_default_response(Gtk.ResponseType.NO)
        yes = dialog.get_widget_for_response(Gtk.ResponseType.YES)
        if yes is not None:
            yes.get_style_context().add_class("destructive-action")
        if dialog.run() == Gtk.ResponseType.YES:
            dialog.destroy()
            self.close()
            return
        dialog.destroy()

    def _next(self, _button=None) -> None:
        if self.page == 0:
            self._show_page(1)
        elif self.page == 1:
            if self.license_check.get_active():
                self._show_page(2)
                self._start_precheck()
        elif self.page == 2:
            if self.precheck_ok:
                self._show_page(3)
        elif self.page == 3:
            if self.installed:
                self._show_page(4)
            else:
                self._ask_password()
        elif self.page == 4:
            self.close()

    def _set_text(self, view: Gtk.TextView, text: str) -> None:
        view.get_buffer().set_text(text)

    def _append_log(self, text: str) -> None:
        if not text:
            return
        view = self.setup_view
        buffer = view.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)
        length = buffer.get_char_count()
        if length > 12000:
            buffer.delete(
                buffer.get_start_iter(),
                buffer.get_iter_at_offset(length - 12000),
            )
        if self._follow_log:
            self._queue_log_scroll()

    def _queue_log_scroll(self) -> None:
        if self._scroll_pending:
            return
        self._scroll_pending = True
        GLib.idle_add(self._scroll_log_to_end)

    def _scroll_log_to_end(self) -> bool:
        self._scroll_pending = False
        adjustment = self.setup_view.get_vadjustment()
        self._auto_scrolling = True
        adjustment.set_value(
            max(0.0, adjustment.get_upper() - adjustment.get_page_size())
        )
        GLib.idle_add(self._finish_auto_scroll)
        return False

    def _finish_auto_scroll(self) -> bool:
        self._auto_scrolling = False
        return False

    def _on_log_scrolled(self, adjustment) -> None:
        if self._auto_scrolling:
            return
        self._follow_log = (
            adjustment.get_value() + adjustment.get_page_size()
            >= adjustment.get_upper() - 4
        )

    def _pulse(self) -> bool:
        if self.busy:
            self.progress.pulse()
            return True
        self.progress.set_fraction(1.0)
        return False

    def _start_busy(self) -> None:
        self.busy = True
        self.progress.set_fraction(0.0)
        self._pulse_id = GLib.timeout_add(110, self._pulse)
        self._sync()

    def _stop_busy(self) -> None:
        self.busy = False
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self._set_progress()
        self._sync()

    def _run_async(
        self,
        command: list[str],
        callback,
        input_text=None,
        on_output=None,
    ) -> None:
        def worker():
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.root),
                    stdin=(
                        subprocess.PIPE
                        if input_text is not None
                        else subprocess.DEVNULL
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                if input_text is not None and process.stdin is not None:
                    process.stdin.write(input_text)
                    process.stdin.close()
                chunks: list[str] = []
                if process.stdout is not None:
                    for line in iter(process.stdout.readline, ""):
                        if not line:
                            break
                        if on_output is not None:
                            GLib.idle_add(on_output, line)
                        else:
                            chunks.append(line)
                    process.stdout.close()
                returncode = process.wait()
                output = "" if on_output is not None else "".join(chunks)
                GLib.idle_add(callback, returncode, output)
            except Exception as exc:  # pragma: no cover - defensive
                GLib.idle_add(callback, 127, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _start_precheck(self) -> None:
        self._set_text(self.precheck_view, "")
        self.precheck_label.set_text("Checking host prerequisites…")
        self._start_busy()
        self._run_async(
            [str(self.root / "setup.sh"), "--precheck"],
            self._precheck_done,
        )

    def _precheck_done(self, rc: int, output: str) -> None:
        self._stop_busy()
        self._set_text(self.precheck_view, output)
        self.precheck_ok = rc == 0
        self.precheck_label.set_text(
            "Precheck passed. Continue to install."
            if self.precheck_ok
            else "Precheck failed. Review the output, fix the host, then retry."
        )
        self._sync()

    def _ask_password(self) -> None:
        dialog = Gtk.Dialog(
            title="Sudo password",
            transient_for=self,
            modal=True,
            flags=0,
        )
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL,
            "Continue", Gtk.ResponseType.OK,
        )
        dialog.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_activates_default(True)
        entry.set_placeholder_text("sudo password")
        box = dialog.get_content_area()
        box.set_border_width(16)
        box.add(Gtk.Label(label="v-EMU needs sudo for host packages and services."))
        box.add(entry)
        dialog.show_all()
        response = dialog.run()
        password = entry.get_text()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self._start_busy()
        self._run_async(
            ["sudo", "-S", "-v"],
            lambda rc, output: self._password_done(rc, output),
            input_text=password + "\n",
        )

    def _password_done(self, rc: int, output: str) -> None:
        if rc != 0:
            self._stop_busy()
            self._message("Sudo authentication failed.\n\n" + output)
            return
        self._start_install()

    def _sudo_keepalive(self) -> None:
        while self._keepalive:
            subprocess.run(
                ["sudo", "-n", "-v"],
                capture_output=True,
                text=True,
                check=False,
            )
            for _ in range(55):
                if not self._keepalive:
                    return
                time.sleep(1)

    def _start_install(self) -> None:
        self._set_text(self.setup_view, "")
        self._follow_log = True
        self._append_log("Running setup.sh --" + self.mode + "\n")
        self._keepalive = True
        threading.Thread(target=self._sudo_keepalive, daemon=True).start()
        self._run_async(
            [str(self.root / "setup.sh"), "--" + self.mode],
            self._install_done,
            on_output=self._append_log,
        )

    def _install_done(self, rc: int, output: str) -> None:
        self._keepalive = False
        self._append_log(output)
        self._stop_busy()
        if rc != 0:
            self._message("Installation failed. Review the setup output.")
            return
        self.installed = True
        self._verify_services()

    def _verify_services(self) -> None:
        units = ["vemud.service"]
        if Path("/etc/debian_version").is_file():
            units.append("libvirtd.service")
        else:
            units.extend(
                [
                    "virtqemud.service",
                    "virtnetworkd.service",
                    "virtstoraged.service",
                    "virtsecretd.service",
                    "virtnodedevd.service",
                ]
            )
        failures = []
        for unit in units:
            enabled = subprocess.run(
                ["sudo", "-n", "systemctl", "is-enabled", unit],
                capture_output=True, text=True, check=False,
            )
            if enabled.returncode != 0:
                subprocess.run(
                    ["sudo", "-n", "systemctl", "enable", unit],
                    capture_output=True, text=True, check=False,
                )
                enabled = subprocess.run(
                    ["sudo", "-n", "systemctl", "is-enabled", unit],
                    capture_output=True, text=True, check=False,
                )
            if enabled.returncode != 0:
                failures.append(f"{unit}: not enabled")
            active = subprocess.run(
                ["sudo", "-n", "systemctl", "is-active", unit],
                capture_output=True, text=True, check=False,
            )
            if active.returncode != 0:
                failures.append(f"{unit}: not active yet")
        if failures:
            self.finish_label.set_text(
                "Installation finished, but service checks need attention:\n"
                + "\n".join(f"• {item}" for item in failures)
            )
        else:
            self.finish_label.set_text(
                "Installation finished. v-EMU and the libvirt services are "
                "enabled and active."
            )
        self._show_page(4)

    def _message(self, text: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text=text,
        )
        dialog.run()
        dialog.destroy()

    def _reboot(self, _button=None) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Reboot now?",
        )
        if dialog.run() != Gtk.ResponseType.YES:
            dialog.destroy()
            return
        dialog.destroy()
        result = subprocess.run(
            ["sudo", "-n", "systemctl", "reboot"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            self._message(
                "Could not reboot automatically. Reboot the computer "
                "manually when convenient."
            )

    def _on_close(self, *_args) -> bool:
        if self.busy:
            self._message(
                "v-EMU is still working. Wait for the current step to finish."
            )
            return True
        Gtk.main_quit()
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--mode", choices=["standard", "full"], default="standard")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if not Path(args.root, "setup.sh").is_file():
        print("setup.sh was not found in the installer payload.", file=sys.stderr)
        return 2
    GLib.set_prgname("org.vemu.VEMU")
    GLib.set_application_name("v-EMU Installer")
    window = InstallerWindow(Path(args.root), args.mode, args.yes)
    window.show_all()
    window._sync()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
