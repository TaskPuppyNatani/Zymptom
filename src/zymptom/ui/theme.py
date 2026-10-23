"""Centralized near-black palette and widget styling; no application logic."""

from importlib.resources import files

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BACKGROUND = "#111111"
SURFACE = "#181818"
SURFACE_RAISED = "#1E1E1E"
BORDER = "#2A2A2A"
TEXT_PRIMARY = "#FAFAFA"
TEXT_SECONDARY = "#D6D6D6"
TEXT_MUTED = "#AFAFAF"
ACCENT = "#8B5CF6"
ACCENT_HOVER = "#A78BFA"
ACCENT_PRESSED = "#6D28D9"
# Deeper fills keep normal-sized light button labels above 4.5:1 contrast.
ACCENT_SOLID = "#7440D8"
ACCENT_SOLID_HOVER = "#8050E4"
SELECTION = "#302340"
ERROR_BACKGROUND = "#291B20"
ERROR_BORDER = "#A96378"
ERROR_TEXT = "#FFC1CC"


def stylesheet() -> str:
    checkmark = files("zymptom.ui").joinpath("assets/check.svg").as_posix()
    return f"""
        QWidget {{
            background-color: {BACKGROUND};
            color: {TEXT_PRIMARY};
            selection-background-color: {SELECTION};
            selection-color: {TEXT_PRIMARY};
        }}
        QLabel {{ background: transparent; }}
        QLabel[tone="secondary"] {{ color: {TEXT_SECONDARY}; }}
        QLabel[tone="muted"] {{ color: {TEXT_MUTED}; }}
        QScrollArea {{ border: none; }}
        QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox {{
            background: {SURFACE};
            border: 1px solid {BORDER};
            border-radius: 6px;
            padding: 6px 10px;
            color: {TEXT_PRIMARY};
            placeholder-text-color: {TEXT_MUTED};
        }}
        QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
        QAbstractSpinBox:focus, QComboBox:focus {{ border: 1px solid {ACCENT_HOVER}; }}
        QComboBox QLineEdit {{ border: none; padding: 0; background: transparent; }}
        QComboBox QAbstractItemView {{
            background: {SURFACE_RAISED};
            color: {TEXT_PRIMARY};
            border: 1px solid {BORDER};
            selection-background-color: {SELECTION};
            selection-color: {TEXT_PRIMARY};
        }}
        QPushButton, QToolButton {{
            background: {SURFACE};
            color: {TEXT_SECONDARY};
            border: 2px solid {BORDER};
            border-radius: 6px;
            padding: 6px 12px;
        }}
        QPushButton:hover, QToolButton:hover {{
            background: {SURFACE_RAISED}; color: {TEXT_PRIMARY};
        }}
        QPushButton:pressed, QToolButton:pressed {{ background: {BORDER}; }}
        QPushButton:checked, QToolButton:checked {{
            background: {SELECTION}; color: {TEXT_PRIMARY}; border-color: {ACCENT};
        }}
        QPushButton[primary="true"] {{
            background: {ACCENT_SOLID}; color: {TEXT_PRIMARY}; border-color: {ACCENT_SOLID};
        }}
        QPushButton[primary="true"]:hover {{
            background: {ACCENT_SOLID_HOVER}; border-color: {ACCENT_SOLID_HOVER};
        }}
        QPushButton[primary="true"]:pressed {{
            background: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED};
        }}
        QPushButton:focus, QToolButton:focus {{ border-color: {ACCENT_HOVER}; }}
        QPushButton[primary="true"]:focus {{ border-color: {TEXT_PRIMARY}; }}
        QPushButton:disabled, QToolButton:disabled {{
            background: {SURFACE}; color: {TEXT_MUTED}; border-color: {BORDER};
        }}
        QCheckBox {{
            spacing: 8px; padding: 4px 6px;
            border: 2px solid transparent; border-radius: 6px;
            color: {TEXT_SECONDARY};
        }}
        QCheckBox:hover {{ background: {SURFACE_RAISED}; }}
        QCheckBox:checked {{ color: {TEXT_PRIMARY}; }}
        QCheckBox:focus {{ border-color: {ACCENT_HOVER}; }}
        QCheckBox::indicator {{
            width: 16px; height: 16px;
            border: 1px solid {TEXT_MUTED}; border-radius: 3px;
            background: {SURFACE};
        }}
        QCheckBox::indicator:checked {{
            background: {ACCENT_PRESSED}; border-color: {ACCENT_HOVER};
            image: url("{checkmark}");
        }}
        QCheckBox::indicator:hover {{ border-color: {ACCENT_HOVER}; }}
        QListWidget {{
            background: {SURFACE}; color: {TEXT_PRIMARY};
            border: 1px solid {BORDER}; border-radius: 6px;
        }}
        QListWidget:focus {{ border-color: {ACCENT_HOVER}; }}
        QListWidget::item {{ padding: 4px 6px; }}
        QListWidget::item:selected {{ background: {SELECTION}; color: {TEXT_PRIMARY}; }}
        QListWidget#episodes {{ background: {BACKGROUND}; border: 2px solid transparent; }}
        QListWidget#episodes:focus {{ border-color: {ACCENT_HOVER}; }}
        QListWidget#episodes::item {{
            background: {SURFACE}; border: 1px solid {BORDER};
            border-radius: 8px; padding: 12px;
        }}
        QListWidget#episodes::item:hover {{ background: {SURFACE_RAISED}; }}
        QListWidget#episodes::item:selected {{
            background: {SELECTION}; border-color: {ACCENT}; color: {TEXT_PRIMARY};
        }}
        QLabel[role="error"] {{
            background: {ERROR_BACKGROUND}; color: {ERROR_TEXT};
            border: 1px solid {ERROR_BORDER}; border-radius: 6px; padding: 10px;
        }}
        QLabel[role="error"]:focus {{ border: 2px solid {ACCENT_HOVER}; }}
        QScrollBar {{ background: {BACKGROUND}; border: none; }}
        QScrollBar::handle {{ background: {BORDER}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
        QScrollBar::handle:hover {{ background: {TEXT_MUTED}; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: {BACKGROUND}; }}
        QToolTip {{
            background: {SURFACE_RAISED}; color: {TEXT_PRIMARY};
            border: 1px solid {BORDER}; padding: 6px;
        }}
    """


def apply_theme(app: QApplication) -> None:
    """Keep the user's font; Fusion supplies consistent native indicators/arrows."""
    app.setStyle("Fusion")
    palette = app.palette()
    for role, color in (
        (QPalette.ColorRole.Window, BACKGROUND),
        (QPalette.ColorRole.WindowText, TEXT_PRIMARY),
        (QPalette.ColorRole.Base, SURFACE),
        (QPalette.ColorRole.AlternateBase, SURFACE_RAISED),
        (QPalette.ColorRole.Text, TEXT_PRIMARY),
        (QPalette.ColorRole.PlaceholderText, TEXT_MUTED),
        (QPalette.ColorRole.Button, SURFACE_RAISED),
        (QPalette.ColorRole.ButtonText, TEXT_PRIMARY),
        (QPalette.ColorRole.Highlight, SELECTION),
        (QPalette.ColorRole.HighlightedText, TEXT_PRIMARY),
        (QPalette.ColorRole.Accent, ACCENT),
        (QPalette.ColorRole.ToolTipBase, SURFACE_RAISED),
        (QPalette.ColorRole.ToolTipText, TEXT_PRIMARY),
        (QPalette.ColorRole.Link, ACCENT_HOVER),
        (QPalette.ColorRole.Light, BORDER),
        (QPalette.ColorRole.Midlight, SURFACE_RAISED),
        (QPalette.ColorRole.Mid, BORDER),
        (QPalette.ColorRole.Dark, BACKGROUND),
        (QPalette.ColorRole.Shadow, BACKGROUND),
    ):
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(TEXT_MUTED))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet())
