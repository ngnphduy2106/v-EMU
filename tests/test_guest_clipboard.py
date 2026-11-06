from __future__ import annotations

import base64
import unittest

from vemu.errors import Conflict
from vemu.guest_clipboard import (
    _user_name,
    get_linux_clipboard,
    get_windows_clipboard,
    set_linux_clipboard,
    set_windows_clipboard,
)


class FakeBackend:
    def __init__(self, os_id: str, users: list[dict] | None = None) -> None:
        self.os_id = os_id
        self.users = users or []
        self.commands: list[str] = []

    def qemu_agent_command(
        self, domain: str, command: dict, *, timeout: int = 30
    ) -> object:
        execute = str(command.get("execute", ""))
        self.commands.append(execute)
        if execute == "guest-get-osinfo":
            return {"id": self.os_id}
        if execute == "guest-get-users":
            return self.users
        raise AssertionError(f"unexpected command: {execute}")


class GuestClipboardTests(unittest.TestCase):
    def test_windows_only(self) -> None:
        backend = FakeBackend("linux")
        with self.assertRaises(Conflict):
            get_windows_clipboard(backend, "vm1")
        with self.assertRaises(Conflict):
            set_windows_clipboard(backend, "vm1", "text")

    def test_no_active_user(self) -> None:
        backend = FakeBackend("mswindows", [])
        with self.assertRaises(Conflict):
            get_windows_clipboard(backend, "vm1")

    def test_latest_user_is_selected(self) -> None:
        backend = FakeBackend(
            "mswindows",
            [
                {"user": "old", "login-time": 10},
                {"user": "current", "login-time": 20},
            ],
        )
        self.assertEqual(_user_name(backend, "vm1"), "current")

    def test_domain_prefix_and_upn_users(self) -> None:
        backend = FakeBackend(
            "mswindows",
            [{"user": r"LAB\ProUser", "login-time": 10}],
        )
        self.assertEqual(_user_name(backend, "vm1"), "ProUser")
        backend = FakeBackend(
            "mswindows",
            [{"user": "user@example.com", "login-time": 10}],
        )
        self.assertEqual(_user_name(backend, "vm1"), "user@example.com")

    def test_unsafe_user_name_is_rejected(self) -> None:
        backend = FakeBackend(
            "mswindows",
            [{"user": "bad/name", "login-time": 10}],
        )
        with self.assertRaises(Conflict):
            _user_name(backend, "vm1")


class LinuxClipboardTests(unittest.TestCase):
    class Backend:
        """Fedora 45 style: utmp is empty, guest-exec and wl-clipboard work."""

        def __init__(self) -> None:
            self.exec_command = ""
            self.input_data: str | None = None

        def qemu_agent_command(
            self, domain: str, command: dict, *, timeout: int = 30
        ) -> object:
            execute = str(command.get("execute", ""))
            arguments = command.get("arguments", {})
            if execute == "guest-get-osinfo":
                return {"id": "fedora"}
            if execute == "guest-get-users":
                return []
            if execute == "guest-exec":
                self.exec_command = " ".join(arguments.get("arg", []))
                self.input_data = arguments.get("input-data")
                return {"pid": 1}
            if execute == "guest-exec-status":
                out = b""
                if "find /run/user -maxdepth 2" in self.exec_command:
                    out = b"1000 wayland-1\n"
                elif "find /run/user/1000" in self.exec_command:
                    out = b"wayland-1\n"
                elif "wl-paste -n" in self.exec_command:
                    out = b"linux clipboard"
                return {
                    "exited": True,
                    "exitcode": 0,
                    "out-data": base64.b64encode(out).decode("ascii"),
                }
            raise AssertionError(f"unexpected command: {execute}")

    def test_wayland_owner_fallback_when_utmp_is_empty(self) -> None:
        backend = self.Backend()
        self.assertEqual(get_linux_clipboard(backend, "vm1"), "linux clipboard")

    def test_set_linux_clipboard_passes_text_via_stdin(self) -> None:
        backend = self.Backend()
        set_linux_clipboard(backend, "vm1", "host clipboard")
        self.assertIsNotNone(backend.input_data)
        self.assertEqual(
            base64.b64decode(backend.input_data).decode("utf-8"),
            "host clipboard",
        )
        self.assertIn("wl-copy", backend.exec_command)


if __name__ == "__main__":
    unittest.main()
