"""Executable content check (FR-SAFE-02): table-driven over built byte fixtures, no binaries."""

import io
import zipfile

import pytest

from dropshare_server.services.file_types import ExecutableKind, executable_kind


def _pe(e_lfanew: int = 0x80) -> bytes:
    dos = bytearray(b"MZ" + b"\x00" * (e_lfanew - 2))
    dos[0x3C:0x40] = e_lfanew.to_bytes(4, "little")
    return bytes(dos) + b"PE\x00\x00" + b"\x4c\x01" + b"\x00" * 64


def _zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", "hello")
    return buffer.getvalue()


def _kind(content: bytes) -> ExecutableKind | None:
    return executable_kind(lambda offset, length: content[offset : offset + length])


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        (_pe(), ExecutableKind.PE),
        (_pe(e_lfanew=0x40), ExecutableKind.PE),  # header right after the DOS header
        (_pe(e_lfanew=0x1000), ExecutableKind.PE),  # beyond the first 64 bytes read
        (b"\x7fELF\x02\x01\x01" + b"\x00" * 60, ExecutableKind.ELF),
        (b"\x7fELF", ExecutableKind.ELF),
        (b"\xfe\xed\xfa\xce" + b"\x00" * 28, ExecutableKind.MACH_O),
        (b"\xfe\xed\xfa\xcf" + b"\x00" * 28, ExecutableKind.MACH_O),
        (b"\xce\xfa\xed\xfe" + b"\x00" * 28, ExecutableKind.MACH_O),
        (b"\xcf\xfa\xed\xfe" + b"\x00" * 28, ExecutableKind.MACH_O),
        (b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\x00" * 40, ExecutableKind.MACH_O),  # 2 archs
        (b"\xca\xfe\xba\xbf\x00\x00\x00\x02" + b"\x00" * 40, ExecutableKind.MACH_O),
    ],
)
def test_programs_are_recognised_whatever_their_name(content: bytes, kind: ExecutableKind) -> None:
    assert _kind(content) == kind


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"M",
        b"MZ",
        b"MZ is how this sentence starts, not a program at all. " * 3,  # no PE header
        _pe()[:0x80],  # cut off before the PE signature
        _pe().replace(b"PE\x00\x00", b"NE\x00\x00"),  # 16-bit Windows: doesn't run any more
        b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\x00" * 40,  # Java 8 class file
        b"\xca\xfe\xba\xbe\x00\x03\x00\x2d" + b"\x00" * 40,  # Java 1.1 class (minor 3)
        b"\xca\xfe\xba\xbe\x00\x00\x00\x00",  # zero architectures
        b"\xca\xfe\xba\xbe",
        b"%PDF-1.7\n" + b"\x00" * 100,
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
        b"\xff\xd8\xff\xe0" + b"\x00" * 100,  # JPEG
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100,  # legacy Office (.doc, .xls)
        _zip(),  # also .docx, .xlsx, .pptx, .jar
        b"plain text\n",
        b"\xef\xbb\xbfELF text with a byte order mark",
        "Résumé".encode("utf-16"),
    ],
)
def test_other_content_is_not_a_program(content: bytes) -> None:
    assert _kind(content) is None


def test_reads_only_the_header_and_the_pe_signature() -> None:
    content = _pe(e_lfanew=0x1000) + b"\x00" * 10_000_000
    reads: list[tuple[int, int]] = []

    def read(offset: int, length: int) -> bytes:
        reads.append((offset, length))
        return content[offset : offset + length]

    assert executable_kind(read) == ExecutableKind.PE
    assert reads == [(0, 64), (0x1000, 4)]
