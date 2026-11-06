"""Client for the v-EMU daemon."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from .errors import VemuError

DEFAULT_SOCKET = "/run/vemu/vemud.sock"


def socket_available(socket_path: str | Path | None = None) -> bool:
    """Return True when the daemon socket is visible.

    ``Path.exists()`` propagates ``PermissionError`` when a parent directory
    is not traversable (for example before the user joins the ``vemu``
    group). Treat that case as "no daemon" instead of crashing the CLI/UI.
    """
    target = Path(socket_path or os.environ.get("VEMU_SOCKET", DEFAULT_SOCKET))
    try:
        return target.exists()
    except OSError:
        return False


class DaemonError(VemuError):
    def __init__(self, type_name: str, message: str) -> None:
        self.type_name = type_name
        super().__init__(message)


class DaemonClient:
    def __init__(
        self,
        socket_path: str | Path | None = None,
        timeout: float = 3600,
    ) -> None:
        self.socket_path = Path(
            socket_path or os.environ.get("VEMU_SOCKET", DEFAULT_SOCKET)
        )
        self.timeout = timeout

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request = {"id": 1, "method": method, "params": params or {}}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(
                    (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
                )
                with connection.makefile("rb") as stream:
                    line = stream.readline()
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError) as exc:
            raise DaemonError(
                "ConnectionError",
                f"Cannot connect to vemud at {self.socket_path}. "
                "Is the daemon running?",
            ) from exc
        except OSError as exc:
            raise DaemonError(
                "ConnectionError", f"Cannot talk to {self.socket_path}: {exc}"
            ) from exc

        if not line:
            raise DaemonError("ProtocolError", "vemud closed the connection.")

        try:
            response = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DaemonError("ProtocolError", "Invalid JSON from vemud.") from exc

        if "error" in response:
            error = response["error"] or {}
            raise DaemonError(
                str(error.get("type", "DaemonError")),
                str(error.get("message", "Unknown daemon error.")),
            )
        return response.get("result")

    def ping(self) -> dict[str, Any]:
        return self.call("ping")

    def status(self) -> dict[str, Any]:
        return self.call("system_info")
