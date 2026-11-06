"""Host prerequisite checks (Doctor) page."""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from .delegate import NoFocusDelegate
from .drift import DriftDialog
from .theme import DARK, LIGHT
from .widgets import EmptyStateTable
from .components import PageHeader
from .tooltips import set_tooltip


class _SelinuxRepairTask(QThread):
    """Relabel every registered storage location in the background."""

    done = Signal(object, object)

    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self._backend = backend

    def run(self) -> None:
        try:
            locations = self._backend.storage_list()
            results: list[dict] = []
            errors: list[dict[str, str]] = []
            for location in locations:
                path = str(location.get("path", ""))
                role = str(location.get("role", "labs"))
                try:
                    payload = self._backend.storage_repair(
                        path, role, force_selinux=True
                    )
                except Exception as exc:  # noqa: BLE001 - report per location
                    errors.append({"path": path, "error": str(exc)})
                else:
                    results.append(payload)
            payload = {
                "locations": results,
                "errors": errors,
                "actions": sum(
                    len(item.get("actions", [])) for item in results
                ),
            }
        except Exception as exc:  # noqa: BLE001 - forwarded to the GUI
            self.done.emit(None, exc)
        else:
            self.done.emit(payload, None)


class DoctorPage(QWidget):
    status = Signal(str)

    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.payload: dict = {}
        self.dark = False
        self._repair_task: _SelinuxRepairTask | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.addWidget(
            PageHeader("Doctor", "Host prerequisites and runtime checks")
        )
        header.addStretch(1)
        self.selinux_button = QPushButton("Repair SELinux…")
        self.selinux_button.setObjectName("SecondaryButton")
        set_tooltip(
            self.selinux_button,
            "Re-apply virt_image_t/virt_content_t labels after enabling "
            "SELinux and rebooting.",
        )
        self.selinux_button.setVisible(False)
        self.selinux_button.setEnabled(False)
        self.selinux_button.clicked.connect(self._repair_selinux)
        header.addWidget(self.selinux_button)
        self.copy_button = QPushButton("Copy report")
        self.copy_button.setObjectName("SecondaryButton")
        self.copy_button.clicked.connect(self._copy_report)
        header.addWidget(self.copy_button)
        self.refresh_button = QPushButton("Run checks")
        self.refresh_button.setObjectName("PrimaryButton")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        self.summary = QLabel("Not checked yet.")
        self.summary.setObjectName("Muted")
        layout.addWidget(self.summary)

        self.security_label = QLabel("Security: not checked")
        self.security_label.setObjectName("Muted")
        layout.addWidget(self.security_label)

        self.drift_label = QLabel("")
        self.drift_label.setObjectName("Warning")
        self.drift_label.setWordWrap(True)
        self.drift_label.hide()
        layout.addWidget(self.drift_label)
        self.drift_button = QPushButton("Review XML drift…")
        self.drift_button.setObjectName("SecondaryButton")
        self.drift_button.setVisible(False)
        self.drift_button.clicked.connect(self._review_drift)
        drift_row = QHBoxLayout()
        drift_row.addWidget(self.drift_button)
        drift_row.addStretch(1)
        layout.addLayout(drift_row)

        self.table = EmptyStateTable(0, 3)
        self.table.setHorizontalHeaderLabels(["STATUS", "CHECK", "DETAIL"])
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setFixedHeight(32)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(0, 90)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(1, 200)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setShowGrid(False)
        self.table.cellDoubleClicked.connect(self._row_double_clicked)
        layout.addWidget(self.table, 1)

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        self._fill()

    def refresh(self) -> None:
        try:
            if hasattr(self.backend, "doctor"):
                self.payload = self.backend.doctor()
            else:
                from ..diagnose import run_checks

                self.payload = run_checks(
                    getattr(self.backend, "uri", "qemu:///system")
                )
        except VemuError as exc:
            self.payload = {
                "ok": False,
                "checks": [
                    {
                        "check": "doctor",
                        "ok": False,
                        "level": "fail",
                        "detail": str(exc),
                    }
                ],
            }
        self._fill()
        failed = sum(
            1
            for item in self.payload.get("checks", [])
            if item.get("level") == "fail"
        )
        warnings = sum(
            1
            for item in self.payload.get("checks", [])
            if item.get("level") == "warn"
        )
        if self.payload.get("ok"):
            message = "All required checks passed."
            if warnings:
                message += f" {warnings} warning(s)."
        else:
            message = f"{failed} check(s) failed."
            if warnings:
                message += f" {warnings} warning(s)."
        self.summary.setText(message)
        self.status.emit(message)

    def _fill(self) -> None:
        checks = self.payload.get("checks", [])
        self.table.setRowCount(0)
        tokens = DARK if self.dark else LIGHT
        for check in checks:
            row = self.table.rowCount()
            self.table.insertRow(row)
            level = str(check.get("level", "ok"))
            if level == "ok":
                label = "OK"
                color = tokens.success
            elif level == "warn":
                label = "WARN"
                color = tokens.warning
            else:
                label = "FAIL"
                color = tokens.danger
            status_item = QTableWidgetItem(label)
            status_item.setForeground(QColor(color))
            detail = str(check.get("detail", ""))
            check_item = QTableWidgetItem(str(check.get("check", "")))
            detail_item = QTableWidgetItem(detail)
            detail_item.setToolTip(detail)
            for item in (status_item, check_item, detail_item):
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
            self.table.setItem(row, 0, status_item)
            self.table.setItem(row, 1, check_item)
            self.table.setItem(row, 2, detail_item)
        self.table.refresh_empty_state()
        self._update_security_controls()
        self._update_drift_controls()
        self.security_label.setText(self._security_summary())

    def _drift_domains(self) -> list[str]:
        for check in self.payload.get("checks", []):
            if check.get("check") != "domain_drift":
                continue
            domains = check.get("domains")
            if isinstance(domains, list):
                return [str(item) for item in domains if item]
            detail = str(check.get("detail", ""))
            marker = "XML drifted from baseline:"
            if marker in detail:
                return [
                    item.strip()
                    for item in detail.split(marker, 1)[1].split(",")
                    if item.strip()
                ]
        return []

    def _update_drift_controls(self) -> None:
        domains = self._drift_domains()
        self._drift = domains
        if domains:
            names = ", ".join(domains)
            self.drift_label.setText(
                f"XML drift detected: {names}. Review the diff or re-pin the "
                "baseline after confirming the change was intentional."
            )
            self.drift_label.show()
            self.drift_button.setVisible(True)
            self.drift_button.setEnabled(
                hasattr(self.backend, "domain_drift")
            )
        else:
            self.drift_label.hide()
            self.drift_button.setVisible(False)
            self.drift_button.setEnabled(False)

    def _review_drift(self) -> None:
        domains = getattr(self, "_drift", [])
        if not domains:
            return
        dialog = DriftDialog(self.backend, domains, self)
        dialog.exec()
        self.refresh()

    def _row_double_clicked(self, row: int, column: int) -> None:
        item = self.table.item(row, 1)
        if item is not None and item.text() == "domain_drift":
            self._review_drift()

    def _selinux_active(self) -> bool:
        security = self.payload.get("security")
        if isinstance(security, dict):
            selinux = security.get("selinux")
            if isinstance(selinux, dict):
                return bool(selinux.get("active"))
        return self._selinux_detail() in {"Enforcing", "Permissive"}

    def _security_summary(self) -> str:
        security = self.payload.get("security")
        if isinstance(security, dict):
            active = str(security.get("active", ""))
            selinux = security.get("selinux") or {}
            if active == "selinux":
                mode = str(selinux.get("mode", "")).strip()
                return f"Security: SELinux {mode}".rstrip()
            if active == "apparmor":
                apparmor = security.get("apparmor") or {}
                profiles = apparmor.get("profiles")
                text = "Security: AppArmor enabled"
                if profiles is not None:
                    text += f" ({profiles} profiles)"
                return text
            if active == "none":
                return "Security: no AppArmor or SELinux LSM active"
        detail = self._selinux_detail()
        if detail in {"Enforcing", "Permissive"}:
            return f"Security: SELinux {detail}"
        for item in self.payload.get("checks", []):
            if item.get("check") == "AppArmor":
                value = str(item.get("detail", "")).strip()
                return f"Security: AppArmor {value}".strip()
        return "Security: unknown"

    def _update_security_controls(self) -> None:
        active = self._selinux_active()
        can_repair = (
            hasattr(self.backend, "storage_list")
            and hasattr(self.backend, "storage_repair")
        )
        self.selinux_button.setVisible(active)
        self.selinux_button.setEnabled(active and can_repair)

    def _selinux_detail(self) -> str:
        for item in self.payload.get("checks", []):
            if item.get("check") == "SELinux":
                return str(item.get("detail", "")).strip()
        return ""

    def _repair_selinux(self) -> None:
        self.refresh()
        detail = self._selinux_detail()
        if detail not in {"Enforcing", "Permissive"}:
            QMessageBox.information(
                self,
                "SELinux repair",
                f"SELinux is currently {detail or 'unknown'}.\n\n"
                "Enable SELinux in the host configuration (for example "
                "SELINUX=enforcing in /etc/selinux/config), reboot, then "
                "run this repair again.",
            )
            return
        try:
            running = [
                domain.name
                for domain in self.backend.list_domains()
                if str(getattr(domain, "state", "")) == "running"
            ]
        except (VemuError, AttributeError) as exc:
            QMessageBox.critical(self, "SELinux repair", str(exc))
            return
        if running:
            QMessageBox.warning(
                self,
                "SELinux repair",
                "Shut down these VMs before repairing SELinux labels:\n\n"
                + "\n".join(f"• {name}" for name in running),
            )
            return
        answer = QMessageBox.question(
            self,
            "Repair SELinux",
            "Re-apply SELinux labels to every registered storage location?\n\n"
            "This runs semanage fcontext and restorecon -R and can take a "
            "few minutes on large storage. Use it after changing SELinux "
            "from Disabled to Enforcing.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.selinux_button.setEnabled(False)
        self.selinux_button.setText("Repairing…")
        self._repair_task = _SelinuxRepairTask(self.backend, self)
        self._repair_task.done.connect(self._repair_done)
        self._repair_task.finished.connect(self._repair_finished)
        self._repair_task.start()

    def _repair_done(self, payload, error) -> None:
        if error is not None:
            QMessageBox.critical(self, "SELinux repair failed", str(error))
            return
        locations = payload.get("locations", [])
        errors = payload.get("errors", [])
        if not locations and not errors:
            QMessageBox.information(
                self,
                "SELinux repair",
                "No storage locations are registered. Add one from Storage "
                "first.",
            )
            return
        text = (
            f"Repaired {len(locations)} storage location(s) "
            f"with {payload.get('actions', 0)} action(s)."
        )
        self.status.emit(text)
        if errors:
            details = "\n".join(
                f"• {item.get('path', '')}: {item.get('error', '')}"
                for item in errors
            )
            QMessageBox.warning(
                self,
                "SELinux repair completed with errors",
                f"{text}\n\nErrors:\n{details}",
            )
        else:
            QMessageBox.information(self, "SELinux repair", text)

    def _repair_finished(self) -> None:
        self.selinux_button.setText("Repair SELinux…")
        if self._repair_task is not None:
            self._repair_task.deleteLater()
            self._repair_task = None
        self.refresh()

    def _copy_report(self) -> None:
        if not self.payload:
            return
        QGuiApplication.clipboard().setText(
            json.dumps(self.payload, indent=2, ensure_ascii=False)
        )
        self.status.emit("Doctor report copied.")
