"""Single-instance guard for the v-EMU desktop UI."""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def default_socket_name() -> str:
    """Return a per-user local socket name."""
    return f"vemu-ui-{os.getuid()}"


class SingleInstanceGuard(QObject):
    """Keep one UI per user and raise the existing window on relaunch."""

    show_requested = Signal()

    def __init__(
        self, *, name: str | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        self.name = name or default_socket_name()
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept_connections)
        self._clients: list[QLocalSocket] = []
        self._listening = False

    def acquire(self) -> bool:
        """Return True when this process owns the instance, False if another runs."""
        probe = QLocalSocket()
        probe.connectToServer(self.name)
        if probe.waitForConnected(250):
            probe.write(b"show\n")
            probe.flush()
            probe.waitForBytesWritten(250)
            probe.disconnectFromServer()
            return False
        probe.abort()
        # Remove a stale socket from a crashed process before listening.
        QLocalServer.removeServer(self.name)
        if self.server.listen(self.name):
            self._listening = True
            return True
        # Lost a startup race with another instance.
        probe = QLocalSocket()
        probe.connectToServer(self.name)
        if probe.waitForConnected(250):
            probe.write(b"show\n")
            probe.flush()
            probe.waitForBytesWritten(250)
            probe.disconnectFromServer()
        probe.abort()
        return False

    def _accept_connections(self) -> None:
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            if client is None:
                continue
            self._clients.append(client)
            client.readyRead.connect(lambda c=client: self._read_client(c))
            client.disconnected.connect(
                lambda c=client: self._drop_client(c)
            )
            self._read_client(client)

    def _read_client(self, client: QLocalSocket) -> None:
        data = bytes(client.readAll()).decode("utf-8", errors="replace")
        if not data:
            return
        if "show" in data.split():
            self.show_requested.emit()
        client.disconnectFromServer()

    def _drop_client(self, client: QLocalSocket) -> None:
        if client in self._clients:
            self._clients.remove(client)
        client.deleteLater()

    def close(self) -> None:
        for client in list(self._clients):
            client.abort()
            client.deleteLater()
        self._clients.clear()
        self.server.close()
        if self._listening:
            QLocalServer.removeServer(self.name)
            self._listening = False
