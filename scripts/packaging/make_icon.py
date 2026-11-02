"""Render the app icon (client resources/brand/logo-mark.svg) into a Windows .ico, for the
executable and the installer (FR-UPD-01). Run by scripts/build_client.ps1:

    uv run python scripts/packaging/make_icon.py build/client/DropShare.ico

Each size is a PNG inside the .ico (supported since Windows Vista), rendered by Qt, so no image
tool is needed.
"""

import os
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
SVG = (
    Path(__file__).resolve().parents[2]
    / "client/src/dropshare_client/resources/brand/logo-mark.svg"
)


def _png(renderer: QSvgRenderer, size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    # PySide6's stubs say bytes, but at runtime only a str format is accepted.
    if not image.save(buffer, "PNG"):  # type: ignore[call-overload]
        raise RuntimeError(f"Couldn't render the {size}px icon")
    return bytes(data.data())


def _ico(images: list[tuple[int, bytes]]) -> bytes:
    """ICONDIR, one ICONDIRENTRY per image (256 is written as 0), then the PNGs."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, payload = b"", b""
    for size, png in images:
        side = size % 256
        entries += struct.pack(
            "<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset + len(payload)
        )
        payload += png
    return header + entries + payload


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: make_icon.py OUTPUT.ico\n")
        return 2
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(["make_icon"])
    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        sys.stderr.write(f"Not a valid SVG: {SVG}\n")
        return 1
    renderer.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
    output = Path(argv[0])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_ico([(size, _png(renderer, size)) for size in SIZES]))
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
