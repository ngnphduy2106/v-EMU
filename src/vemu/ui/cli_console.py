"""In-app console for the v-EMU command line interface."""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

import vemu
from PySide6.QtCore import QEvent, QProcess, QProcessEnvironment, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .dialogs import VemuDialog, dialog_layout
from .fonts import mono_font

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

QUICK_HELP = """\
Common commands (the leading "vemu" is optional):
  list                         list virtual machines
  list --json                  machine-readable output
  init [path]                  create a sample lab.yaml
  start --domain NAME          start a VM
  shutdown --domain NAME       graceful ACPI shutdown
  console --domain NAME        print VNC connection details
  ssh NAME --user lab          open an SSH session
  stats --domain NAME          CPU and memory usage
  snapshot list --domain NAME  show snapshot layers
  export --ansible-inventory --dest inventory.yml
  export --libvirt-xml --dest DIR
  template list --root DIR     list templates
  net status                   network/bridge runtime
  gc [root] [--force]          clean orphan chain layers
  doctor                       check the host configuration
  doctor --repair --dry-run    show storage repair plan
  media list --domain NAME     show CD/DVD media
  help                         this quick reference
  --help                       full CLI reference
  clear                        clear this console
  exit                         close the console
"""


class CliConsoleDialog(VemuDialog):
    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.process: QProcess | None = None
        self.history: list[str] = []
        self.history_index = 0
        self._running = False
        self.setWindowTitle("v-EMU console")
        self.resize(900, 600)

        layout = dialog_layout(self)
        title = QLabel("v-EMU console")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Run v-EMU commands against the same daemon connection as this "
            "window. Output is read-only; use CLI flags such as --json for "
            "scripts."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        mono = mono_font(10)
        self.output = QPlainTextEdit()
        self.output.setObjectName("CliOutput")
        self.output.setReadOnly(True)
        self.output.setFont(mono)
        self.output.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
        )
        self.output.setMaximumBlockCount(5000)
        layout.addWidget(self.output, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        prompt = QLabel("vemu ›")
        prompt.setObjectName("CliPrompt")
        prompt.setFont(mono)
        input_row.addWidget(prompt)
        self.input = QLineEdit()
        self.input.setObjectName("CliInput")
        self.input.setFont(mono)
        self.input.setPlaceholderText("Type a command, e.g. list")
        self.input.returnPressed.connect(self._submit)
        self.input.installEventFilter(self)
        input_row.addWidget(self.input, 1)
        self.run_button = QPushButton("Run")
        self.run_button.setObjectName("PrimaryButton")
        self.run_button.clicked.connect(self._submit)
        input_row.addWidget(self.run_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("SecondaryButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        input_row.addWidget(self.stop_button)
        layout.addLayout(input_row)

        footer = QHBoxLayout()
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("Muted")
        footer.addWidget(self.status_label)
        footer.addStretch(1)
        clear = QPushButton("Clear")
        clear.setObjectName("SecondaryButton")
        clear.clicked.connect(self.output.clear)
        footer.addWidget(clear)
        close = QPushButton("Close")
        close.setObjectName("SecondaryButton")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

        self._print(
            "v-EMU console · type 'help' for commands, 'exit' to close."
        )
        self._print("")
        self.input.setFocus()

    # ------------------------------------------------------------------ output

    def _print(self, text: str = "") -> None:
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n")
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def _stream(self, text: str) -> None:
        if not text:
            return
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    @staticmethod
    def _clean(text: str) -> str:
        text = ANSI_RE.sub("", text)
        return text.replace("\r\n", "\n").replace("\r", "")

    # ------------------------------------------------------------------ command

    def _submit(self) -> None:
        if self._running:
            return
        command = self.input.text().strip()
        if not command:
            return
        self.input.clear()
        self.history.append(command)
        self.history_index = len(self.history)
        lowered = command.lower()
        if lowered in {"exit", "quit"}:
            self.accept()
            return
        if lowered == "clear":
            self.output.clear()
            return
        if lowered in {"help", "?"}:
            self._print(QUICK_HELP.rstrip())
            return
        tokens = shlex.split(command)
        if tokens and tokens[0] == "vemu":
            tokens = tokens[1:]
        if not tokens:
            self._print(QUICK_HELP.rstrip())
            return
        self._execute(tokens)

    def _execute(self, arguments: list[str]) -> None:
        if self.process is not None:
            self._print("[a command is already running]")
            return
        rendered = " ".join(shlex.quote(item) for item in arguments)
        self._print(f"$ vemu {rendered}")
        process = QProcess(self)
        process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        process.setProcessEnvironment(self._process_environment())
        process.setWorkingDirectory(str(Path.cwd()))
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._process_error)
        self.process = process
        self._set_running(True)
        process.start(sys.executable, ["-m", "vemu", *arguments])
        if not process.waitForStarted(3000):
            self._print("[could not start the vEMU CLI]")
            self._set_running(False)
            self.process = None

    def _process_environment(self) -> QProcessEnvironment:
        environment = QProcessEnvironment.systemEnvironment()
        package_root = Path(vemu.__file__).resolve().parent.parent
        existing = environment.value("PYTHONPATH")
        paths = [str(package_root)]
        if existing:
            paths.append(existing)
        environment.insert("PYTHONPATH", os.pathsep.join(paths))
        environment.insert("PYTHONUNBUFFERED", "1")
        client = getattr(self.backend, "client", None)
        socket_path = getattr(client, "socket_path", None)
        if socket_path:
            environment.insert("VEMU_SOCKET", str(socket_path))
        return environment

    def _read_output(self) -> None:
        if self.process is None:
            return
        raw = bytes(self.process.readAllStandardOutput())
        self._stream(self._clean(raw.decode("utf-8", errors="replace")))

    def _finished(self, code: int, _status) -> None:
        self._read_output()
        if self._running:
            self._print(f"[exit {code}]" if code else "[done]")
        self._set_running(False)
        self.process = None
        self.input.setFocus()

    def _process_error(self, _error) -> None:
        if self.process is not None:
            self._print(f"[process error: {self.process.errorString()}]")

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.run_button.setEnabled(not running)
        self.input.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.status_label.setText("Running…" if running else "Ready")

    def _stop(self) -> None:
        if self.process is None:
            return
        self._print("[stopping]")
        self.process.kill()

    # ------------------------------------------------------------------ events

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt API
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Up:
                if self.history:
                    self.history_index = max(0, self.history_index - 1)
                    self.input.setText(self.history[self.history_index])
                return True
            if event.key() == Qt.Key.Key_Down:
                if self.history:
                    self.history_index = min(
                        len(self.history), self.history_index + 1
                    )
                    if self.history_index == len(self.history):
                        self.input.clear()
                    else:
                        self.input.setText(self.history[self.history_index])
                return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.process is not None:
            self.process.kill()
            self.process.waitForFinished(500)
            self.process = None
        super().closeEvent(event)
