from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QLabel


def test_branding_footer_text_is_green_without_recoloring_other_labels(window, qapp):
    name = next(label for label in window.branding.findChildren(QLabel)
                if label.text() == "TaskPuppy Kreations")
    logo = next(label for label in window.branding.findChildren(QLabel)
                if label.accessibleName() == "TaskPuppy Kreations logo")
    assert name.palette().color(QPalette.ColorRole.WindowText) == QColor("#39FF14")
    assert name.font().pointSizeF() == window.font().pointSizeF()
    assert name.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert not logo.pixmap().isNull()

    ordinary = QLabel("TaskPuppy Kreations", window)
    ordinary.ensurePolished()
    assert ordinary.palette().color(QPalette.ColorRole.WindowText) == QColor("#FAFAFA")
    assert window.heading.palette().color(QPalette.ColorRole.WindowText) == QColor("#FAFAFA")
    assert qapp.palette().color(QPalette.ColorRole.Window) == QColor("#111111")
    assert qapp.palette().color(QPalette.ColorRole.Accent) == QColor("#8B5CF6")
    assert qapp.palette().color(QPalette.ColorRole.Highlight) == QColor("#302340")
    assert qapp.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text) == QColor("#AFAFAF")
