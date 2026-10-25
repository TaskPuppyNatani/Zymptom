"""Load the supplied PNG from package resources, independent of the launch path."""

from functools import lru_cache
from importlib.resources import files

from PySide6.QtCore import Qt
from PySide6.QtGui import QBitmap, QIcon, QImage, QPixmap, QRegion
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget


@lru_cache(maxsize=1)
def logo_pixmap() -> QPixmap:
    image = QImage.fromData(files("zymptom.ui").joinpath("assets/taskpuppy_kreations_logo.png").read_bytes())
    if image.isNull():
        raise RuntimeError("The TaskPuppy Kreations logo could not be loaded.")
    # Remove transparent padding in memory so the small mark/icon remains legible.
    # The packaged PNG stays byte-identical to the original branding asset.
    bounds = QRegion(QBitmap.fromImage(image.createAlphaMask())).boundingRect()
    return QPixmap.fromImage(image.copy(bounds))


def application_icon() -> QIcon:
    return QIcon(logo_pixmap())


def brand_mark(parent: QWidget) -> QWidget:
    mark = QWidget(parent)
    row = QHBoxLayout(mark)
    row.setContentsMargins(0, 4, 0, 0)
    row.setSpacing(8)
    logo = QLabel()
    logo.setAccessibleName("TaskPuppy Kreations logo")
    height = mark.fontMetrics().height() * 3
    ratio = mark.devicePixelRatioF()
    pixmap = logo_pixmap().scaled(
        round(height * ratio), round(height * ratio),
        Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
    )
    pixmap.setDevicePixelRatio(ratio)
    logo.setPixmap(pixmap)
    row.addWidget(logo)
    name = QLabel("TaskPuppy Kreations")
    name.setProperty("role", "brand-name")
    row.addWidget(name)
    row.addStretch()
    return mark
