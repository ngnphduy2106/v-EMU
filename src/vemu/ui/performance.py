"""Performance tuning wizard for Linux guests."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..errors import VemuError
from .dialogs import VemuDialog, dialog_layout


class PerformanceTuningDialog(VemuDialog):
    """Precheck then apply offline guest tuning + virtio-gpu."""

    def __init__(self, backend, domain_name: str, parent=None) -> None:
        super().__init__(parent, title="Performance tuning")
        self.backend = backend
        self.domain_name = domain_name
        self.payload: dict | None = None
        self.changed = False

        self.setMinimumWidth(640)
        self.resize(680, 500)
        layout = dialog_layout(self)

        title = QLabel(f"Performance tuning for {domain_name}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        intro = QLabel(
            "This wizard makes the noVNC desktop smoother by switching the "
            "guest to virtio-gpu and applying offline Linux guest tuning:\n\n"
            "• disable guest suspend/hibernate;\n"
            "• mask the systemd sleep targets;\n"
            "• add a small boot-time CPU/sysctl tuning unit.\n\n"
            "The VM must be Linux with systemd, non-immutable, and completely "
            "stopped. The v-EMU service performs the privileged disk and XML "
            "changes as root; no sudo password is stored or sent."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.status = QLabel("Run the precheck to inspect this guest.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.details = QLabel()
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.details, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("SecondaryButton")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.precheck_button = QPushButton("Run precheck")
        self.precheck_button.setObjectName("PrimaryButton")
        self.precheck_button.clicked.connect(self._run_precheck)
        buttons.addWidget(self.precheck_button)
        self.apply_button = QPushButton("Apply tuning")
        self.apply_button.setObjectName("PrimaryButton")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

    def _set_wait(self, waiting: bool) -> None:
        self.precheck_button.setEnabled(not waiting)
        self.apply_button.setEnabled(
            not waiting and bool((self.payload or {}).get("supported"))
        )
        if waiting:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()
        QApplication.processEvents()

    def _safe_probe(self) -> dict | None:
        try:
            return self.backend.performance_probe(self.domain_name)
        except (VemuError, AttributeError) as exc:
            QMessageBox.critical(self, "Precheck failed", str(exc))
            return None

    def _run_precheck(self) -> None:
        self.payload = None
        self.apply_button.setEnabled(False)
        self._set_wait(True)
        try:
            payload = self._safe_probe()
        finally:
            self._set_wait(False)
        if payload is None:
            return
        self.payload = payload
        self._render_result(payload)

    def _render_result(self, payload: dict) -> None:
        supported = bool(payload.get("supported"))
        distro = str(payload.get("distro") or "Linux guest")
        systemd = "yes" if payload.get("systemd") else "no"
        immutable = "yes" if payload.get("immutable") else "no"
        tested = "yes" if payload.get("tested") else "no"
        video = str(payload.get("video") or "unknown")
        change = "yes" if payload.get("needs_video_change") else "no"
        tuned = "yes" if payload.get("tuned") else "no"
        reason = str(payload.get("reason") or "")
        warnings = payload.get("warnings") or []
        if supported:
            self.status.setText(
                "Precheck passed. Review the changes and apply when ready."
            )
            self.details.setText(
                f"<b>Guest:</b> {distro}<br>"
                f"<b>systemd:</b> {systemd} · <b>immutable:</b> {immutable} · "
                f"<b>tested:</b> {tested}<br>"
                f"<b>Video:</b> {video} · <b>switch to virtio-gpu:</b> {change}<br>"
                f"<b>tuned present:</b> {tuned} (optional, not required)<br><br>"
                "<b>Will apply:</b> disable sleep/hibernate, mask sleep "
                "targets, add sysctl tuning"
                + (
                    ", and switch the domain XML to virtio-gpu."
                    if payload.get("needs_video_change")
                    else "."
                )
                + (
                    "<br><br><b>Warnings:</b> " + "<br>".join(
                        str(item) for item in warnings
                    )
                    if warnings
                    else ""
                )
            )
        else:
            self.status.setText("Precheck did not pass.")
            self.details.setText(
                f"<b>Guest:</b> {distro}<br>"
                f"<b>systemd:</b> {systemd} · <b>immutable:</b> {immutable} · "
                f"<b>tested:</b> {tested}<br><br>"
                f"<b>Reason:</b> {reason or 'Guest is not supported.'}"
            )
        self.apply_button.setEnabled(supported)

    def _apply(self) -> None:
        if not (self.payload or {}).get("supported"):
            return
        self._set_wait(True)
        error: str | None = None
        result: dict = {}
        try:
            result = self.backend.performance_apply(self.domain_name)
        except (VemuError, AttributeError) as exc:
            error = str(exc)
        finally:
            self._set_wait(False)
        if error is not None:
            QMessageBox.critical(self, "Tuning failed", error)
            return
        self.changed = True
        changed = bool(result.get("video_changed"))
        note = (
            "Guest tuning applied and video switched to virtio-gpu."
            if changed
            else "Guest tuning applied; video was already virtio-gpu."
        )
        QMessageBox.information(self, "Performance tuning", note)
        self.accept()
