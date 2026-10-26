"""Fit the existing TaskPuppy artwork into PWA icon canvases; no new artwork."""

from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter

ROOT = Path(__file__).resolve().parents[1]
source = QImage(str(ROOT.parent / "assets/branding/taskpuppy_kreations_logo.png"))
if source.isNull():
    raise SystemExit("Existing branding image is missing")
rgba = source.convertToFormat(QImage.Format.Format_RGBA8888)
pixels = bytes(rgba.constBits())
width, height = rgba.width(), rgba.height()
points = [(index % width, index // width) for index, alpha in enumerate(pixels[3::4]) if alpha]
left = min(x for x, _ in points)
right = max(x for x, _ in points)
top = min(y for _, y in points)
bottom = max(y for _, y in points)
art = source.copy(left, top, right - left + 1, bottom - top + 1)
directory = ROOT / "public/icons"
directory.mkdir(parents=True, exist_ok=True)
for filename, size, scale in (("icon-192.png", 192, .86), ("icon-512.png", 512, .86),
                             ("maskable-512.png", 512, .56), ("apple-touch-icon.png", 180, .8)):
    canvas = QImage(size, size, QImage.Format.Format_ARGB32)
    canvas.fill(QColor("#111111"))
    fitted = art.scaled(int(size * scale), int(size * scale), Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    painter = QPainter(canvas)
    painter.drawImage((size - fitted.width()) // 2, (size - fitted.height()) // 2, fitted)
    painter.end()
    if not canvas.save(str(directory / filename)):
        raise SystemExit(f"Could not create {filename}")
print("Generated PWA icons from existing TaskPuppy branding")
