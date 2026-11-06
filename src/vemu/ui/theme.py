"""Design tokens and Qt stylesheet generation.

Tokens are aligned with the CoreSystem visual language (see docs/DESIGN.md):
teal accent, neutral surfaces, border-first separation, light and dark parity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .tokens import CONTROL, RADIUS, SPACE, TYPE


ASSETS = Path(__file__).with_name("assets")


@dataclass(frozen=True)
class Tokens:
    bg: str
    surface: str
    hover: str
    border: str
    border_strong: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_subtle: str
    on_accent: str
    success: str
    warning: str
    danger: str


LIGHT = Tokens(
    bg="#ffffff",
    surface="#f6f7f7",
    hover="#f0f1f1",
    border="#e4e7e7",
    border_strong="#b3c0c0",
    text="#1a1a1a",
    muted="#5f6b6b",
    accent="#0f766e",
    accent_hover="#115e59",
    accent_subtle="#e6f4f1",
    on_accent="#ffffff",
    success="#15803d",
    warning="#b45309",
    danger="#b91c1c",
)

DARK = Tokens(
    bg="#181818",
    surface="#1e1e1e",
    hover="#242424",
    border="#2c2c2c",
    border_strong="#4c4c4c",
    text="#f0f0f0",
    muted="#a0a0a0",
    accent="#2dd4bf",
    accent_hover="#5eead4",
    accent_subtle="#16302c",
    on_accent="#042f2e",
    success="#4ade80",
    warning="#fbbf24",
    danger="#f87171",
)


def stylesheet(tokens: Tokens) -> str:
    t = tokens
    # Qt style sheets do not load SVG assets through file:// URIs, so use the
    # plain filesystem path (quoted to allow spaces in the install directory).
    check = str(
        ASSETS
        / ("check-white.svg" if t.on_accent.lower() == "#ffffff" else "check-dark.svg")
    )
    return f"""
    QMainWindow, QWidget {{
        background: {t.bg};
        color: {t.text};
    }}
    QLabel {{
        background: transparent;
    }}
    QDialog {{
        background: {t.bg};
    }}
    QPushButton {{
        background: {t.surface};
        color: {t.text};
        border: 1px solid {t.border_strong};
        border-radius: {RADIUS.sm}px;
        padding: {CONTROL.button_padding_y}px {CONTROL.button_padding_x}px;
        font-weight: 500;
    }}
    QPushButton:hover {{
        background: {t.hover};
        border-color: {t.border_strong};
    }}
    QPushButton:pressed {{
        background: {t.border};
    }}
    QPushButton:focus {{
        border-color: {t.accent};
    }}
    QPushButton:default {{
        background: {t.accent};
        color: {t.on_accent};
        border-color: {t.accent};
        font-weight: 600;
    }}
    QPushButton:default:hover {{
        background: {t.accent_hover};
        border-color: {t.accent_hover};
    }}
    QPushButton:default:pressed {{
        background: {t.accent_hover};
        border-color: {t.accent_hover};
    }}
    QPushButton:disabled {{
        background: {t.surface};
        color: {t.muted};
        border-color: {t.border};
    }}
    QPushButton:default:disabled {{
        background: {t.hover};
        color: {t.muted};
        border-color: {t.border};
    }}
    QDialogButtonBox {{
        dialogbuttonbox-buttons-have-icons: 0;
    }}
    QMessageBox {{
        background: {t.bg};
    }}
    QMessageBox QLabel {{
        color: {t.text};
    }}
    QMessageBox QTextEdit {{
        background: {t.surface};
        color: {t.text};
        border: 1px solid {t.border};
    }}
    QMessageBox QPushButton {{
        min-width: 72px;
    }}
    QLabel#DialogTitle {{
        font-size: {TYPE.section}px;
        font-weight: 600;
    }}
    QLabel#CardTitle {{
        font-size: {TYPE.body_lg}px;
        font-weight: 600;
    }}
    QLabel#HeroTitle {{
        font-size: {TYPE.hero}px;
        font-weight: 600;
    }}
    QGroupBox {{
        border: 1px solid {t.border};
        border-radius: {RADIUS.lg}px;
        margin-top: {SPACE.md}px;
        padding: {SPACE.md}px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: {SPACE.md}px;
        padding: 0 {SPACE.xs}px;
        background: {t.bg};
        color: {t.muted};
    }}
    QGroupBox QLabel {{
        background: transparent;
    }}
    QWidget#TopBar {{
        background: {t.bg};
        border-bottom: 1px solid {t.border};
    }}
    QWidget#Sidebar {{
        background: {t.surface};
        border-right: 1px solid {t.border};
    }}
    QLabel#Brand {{
        font-size: {TYPE.body_lg}px;
        font-weight: 700;
        letter-spacing: 0.04em;
    }}
    QLabel#BrandTagline {{
        color: {t.muted};
        font-size: {TYPE.small}px;
        padding-top: 2px;
    }}
    QLabel#SectionLabel {{
        color: {t.muted};
        font-size: {TYPE.micro}px;
        font-weight: 700;
        letter-spacing: 0.08em;
    }}
    QLabel#PageTitle {{
        font-size: {TYPE.title}px;
        font-weight: 700;
    }}
    QLabel#PageSubtitle, QLabel#Muted {{
        color: {t.muted};
        font-size: {TYPE.body_sm}px;
    }}
    QLabel#KpiLabel {{
        color: {t.muted};
        font-size: {TYPE.micro}px;
        font-weight: 700;
        letter-spacing: 0.08em;
    }}
    QLabel#KpiValue {{
        font-size: {TYPE.title}px;
        font-weight: 700;
    }}
    QFrame#Card, QFrame#InspectorCard {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: {RADIUS.xl}px;
    }}
    QFrame#SetupBanner {{
        background: {t.accent_subtle};
        border: 1px solid {t.accent};
        border-radius: {RADIUS.lg}px;
    }}
    QFrame#SetupBanner QLabel {{
        background: transparent;
        color: {t.text};
    }}
    QFrame#Inspector {{
        background: {t.surface};
        border-left: 1px solid {t.border};
    }}
    QWidget#MiniConsole {{
        background: #000000;
        border: 1px solid {t.border};
        border-radius: {RADIUS.sm}px;
    }}
    QWidget#MiniConsole QLabel {{
        color: #a8b0b0;
        background: transparent;
    }}
    QWidget#MiniConsoleOverlay {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: {RADIUS.sm}px;
    }}
    QWidget#MiniConsoleOverlay:hover {{
        border: 1px solid {t.accent};
    }}
    QLabel#MiniConsoleChip {{
        background: rgba(0, 0, 0, 150);
        color: #ffffff;
        border-radius: {RADIUS.xs}px;
        padding: 2px 6px;
        font-size: {TYPE.micro}px;
    }}
    QPushButton#NavButton {{
        text-align: left;
        padding: 7px 10px;
        border: none;
        border-radius: {RADIUS.sm}px;
        background: transparent;
        color: {t.muted};
        font-size: {TYPE.body}px;
    }}
    QPushButton#NavButton:hover {{
        background: {t.hover};
        color: {t.text};
    }}
    QPushButton#IconButton {{
        background: transparent;
        color: {t.muted};
        border: none;
        border-radius: {RADIUS.sm}px;
        padding: 0;
        font-size: {TYPE.body_lg}px;
    }}
    QPushButton#IconButton:hover {{
        background: {t.hover};
        color: {t.text};
    }}
    QPushButton#IconButton:pressed {{
        background: {t.border};
    }}
    QPushButton#NavButton[selected="true"] {{
        background: {t.accent_subtle};
        color: {t.accent};
        font-weight: 600;
    }}
    QPushButton#PrimaryButton {{
        background: {t.accent};
        color: {t.on_accent};
        border: none;
        border-radius: {RADIUS.sm}px;
        padding: 7px 14px;
        font-weight: 600;
    }}
    QPushButton#PrimaryButton:hover {{
        background: {t.accent_hover};
    }}
    QPushButton#PrimaryButton:pressed {{
        background: {t.accent_hover};
    }}
    QPushButton#PrimaryButton:disabled {{
        background: {t.hover};
        color: {t.muted};
    }}
    QPushButton#SecondaryButton {{
        background: transparent;
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: {RADIUS.sm}px;
        padding: 7px 12px;
    }}
    QPushButton#SecondaryButton:hover {{
        background: {t.hover};
    }}
    QPushButton#SecondaryButton:pressed {{
        background: {t.border};
    }}
    QPushButton#SecondaryButton:disabled {{
        color: {t.muted};
    }}
    QToolTip {{
        background-color: {t.surface};
        color: {t.text};
        border: 1px solid {t.border_strong};
        border-radius: {RADIUS.sm}px;
        padding: 6px 8px;
        font-size: {TYPE.body_sm}px;
    }}
    QLabel#Warning {{
        color: {t.warning};
        font-size: {TYPE.body_sm}px;
    }}
    QPushButton#InspectorAction {{
        background: transparent;
        border: 1px solid {t.border};
        border-radius: {RADIUS.md}px;
        padding: 0;
    }}
    QPushButton#InspectorAction:hover {{
        background: {t.hover};
        border-color: {t.border_strong};
    }}
    QPushButton#InspectorAction:pressed {{
        background: {t.border};
    }}
    QPushButton#InspectorAction:disabled {{
        border-color: {t.border};
        background: transparent;
    }}
    QPushButton#InspectorAction[danger="true"]:hover {{
        background: {t.hover};
        border-color: {t.danger};
    }}
    QPushButton#DangerButton {{
        background: transparent;
        color: {t.danger};
        border: 1px solid {t.border};
        border-radius: {RADIUS.sm}px;
        padding: 7px 12px;
    }}
    QPushButton#DangerButton:hover {{
        background: {t.hover};
        border-color: {t.danger};
    }}
    QLineEdit#Search {{
        background: {t.bg};
        border: 1px solid {t.border};
        border-radius: {RADIUS.sm}px;
        padding: 3px 10px;
        min-height: 0;
        max-height: 30px;
        selection-background-color: {t.accent_subtle};
        selection-color: {t.text};
    }}
    QLineEdit#Search:focus {{
        border-color: {t.accent};
    }}
    QLineEdit, QComboBox, QTextEdit, QSpinBox, QDoubleSpinBox {{
        background: {t.bg};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: {RADIUS.sm}px;
        padding: 5px 8px;
        selection-background-color: {t.accent_subtle};
        selection-color: {t.text};
    }}
    QLineEdit, QSpinBox, QDoubleSpinBox {{
        min-height: 20px;
        max-height: 20px;
    }}
    QLineEdit:focus, QComboBox:focus, QTextEdit:focus,
    QSpinBox:focus, QDoubleSpinBox:focus {{
        border-color: {t.accent};
    }}
    QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled,
    QSpinBox:disabled, QDoubleSpinBox:disabled {{
        background: {t.surface};
        color: {t.muted};
    }}
    QComboBox {{
        min-height: 30px;
        max-height: 30px;
        padding: 0 8px;
    }}
    QSpinBox, QDoubleSpinBox {{
        padding: 5px 8px;
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 24px;
        padding: 2px 6px;
    }}
    QComboBox QAbstractItemView {{
        background: {t.surface};
        color: {t.text};
        border: 1px solid {t.border_strong};
        selection-background-color: {t.accent_subtle};
        selection-color: {t.text};
    }}
    QFileDialog {{
        background: {t.bg};
        color: {t.text};
    }}
    QFileDialog QLabel {{
        color: {t.text};
    }}
    QFileDialog QTreeView, QFileDialog QListView, QFileDialog QTableView {{
        background: {t.bg};
        alternate-background-color: {t.surface};
        color: {t.text};
        border: 1px solid {t.border};
        selection-background-color: {t.accent_subtle};
        selection-color: {t.text};
    }}
    QFileDialog QTreeView::item:selected,
    QFileDialog QListView::item:selected,
    QFileDialog QTableView::item:selected {{
        background: {t.accent};
        color: {t.on_accent};
    }}
    QFileDialog QHeaderView::section {{
        background: {t.surface};
        color: {t.muted};
        border: none;
        border-bottom: 1px solid {t.border};
        padding: 4px 8px;
        font-size: {TYPE.micro}px;
        font-weight: 700;
    }}
    QFileDialog QToolButton {{
        background: transparent;
        color: {t.text};
        border: 1px solid transparent;
        border-radius: {RADIUS.xs}px;
        padding: 2px;
    }}
    QFileDialog QToolButton:hover {{
        background: {t.hover};
        border-color: {t.border};
    }}
    QFileDialog QSplitter::handle {{
        background: {t.border};
    }}
    QCheckBox {{
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {t.border_strong};
        border-radius: {RADIUS.xs}px;
        background: {t.bg};
    }}
    QCheckBox::indicator:hover {{
        border-color: {t.accent};
    }}
    QCheckBox::indicator:checked {{
        background: {t.accent};
        border-color: {t.accent};
        image: url("{check}");
    }}
    QCheckBox::indicator:disabled {{
        background: {t.surface};
        border-color: {t.border};
    }}
    QCheckBox::indicator:checked:disabled {{
        background: {t.surface};
        border-color: {t.border_strong};
        image: url("{check}");
    }}
    QTableWidget {{
        background: {t.bg};
        alternate-background-color: {t.bg};
        border: 1px solid {t.border};
        border-radius: {RADIUS.xl}px;
        gridline-color: transparent;
        selection-background-color: {t.accent_subtle};
        selection-color: {t.text};
        outline: 0;
    }}
    QTableWidget::item:focus {{
        border: none;
        outline: 0;
    }}
    QTableWidget::item {{
        padding: 0 8px;
        border-bottom: 1px solid {t.border};
    }}
    QTableWidget::item:hover {{
        background: {t.hover};
    }}
    QTableWidget::item:selected {{
        background: {t.accent_subtle};
        color: {t.text};
    }}
    QTableWidget::item:selected:hover {{
        background: {t.accent_subtle};
    }}
    QLabel#EmptyStateTitle {{
        font-size: {TYPE.body_lg}px;
        font-weight: 600;
    }}
    QLabel#EmptyStateBody {{
        color: {t.muted};
        font-size: {TYPE.body_sm}px;
    }}
    QTextBrowser#GuideBrowser {{
        background: {t.bg};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: {RADIUS.xl}px;
        padding: 16px;
        selection-background-color: {t.accent_subtle};
        selection-color: {t.text};
    }}
    QTextBrowser#ReportBrowser {{
        background: {t.bg};
        color: {t.text};
        border: none;
        padding: 0;
        selection-background-color: {t.accent_subtle};
        selection-color: {t.text};
    }}
    QProgressBar {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 2px;
    }}
    QProgressBar::chunk {{
        background: {t.accent};
        border-radius: 2px;
    }}
    QPlainTextEdit#CliOutput {{
        background: {t.bg};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: {RADIUS.lg}px;
        padding: 8px;
        selection-background-color: {t.accent};
        selection-color: {t.on_accent};
    }}
    QLineEdit#CliInput {{
        background: {t.surface};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: {RADIUS.sm}px;
        padding: 6px 8px;
    }}
    QLineEdit#CliInput:focus {{
        border-color: {t.accent};
    }}
    QLabel#CliPrompt {{
        color: {t.accent};
        font-weight: 600;
    }}
    QHeaderView::section {{
        background: {t.surface};
        color: {t.muted};
        border: none;
        border-bottom: 1px solid {t.border};
        padding: 0 8px 0 12px;
        font-size: {TYPE.micro}px;
        font-weight: 700;
    }}
    QMenu {{
        background: {t.surface};
        color: {t.text};
        border: 1px solid {t.border_strong};
        border-radius: {RADIUS.lg}px;
        padding: 5px;
    }}
    QMenu::item {{
        padding: 6px 24px 6px 12px;
        border-radius: {RADIUS.xs}px;
        background: transparent;
    }}
    QMenu::item:selected {{
        background: {t.accent_subtle};
        color: {t.text};
    }}
    QMenu::item:disabled {{
        color: {t.muted};
    }}
    QMenu::separator {{
        height: 1px;
        background: {t.border};
        margin: 4px 8px;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {t.border};
        border-radius: 5px;
        min-height: 24px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QStatusBar {{
        background: {t.bg};
        color: {t.muted};
        border-top: 1px solid {t.border};
    }}
    """
