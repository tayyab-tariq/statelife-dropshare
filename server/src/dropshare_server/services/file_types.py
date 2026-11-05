"""Recognise programs by their first bytes, whatever the file is called (FR-SAFE-02, OQ-56).

Pure functions over bytes: the caller reads them from storage.
"""

from collections.abc import Callable
from enum import StrEnum

# Enough for every check below except the PE signature, whose offset is read from here.
HEADER_BYTES = 64

_PE_OFFSET_FIELD = slice(0x3C, 0x40)  # e_lfanew in the DOS header: where "PE\0\0" is
_MACH_O_THIN = frozenset(
    {
        b"\xfe\xed\xfa\xce",  # 32-bit, big-endian
        b"\xfe\xed\xfa\xcf",  # 64-bit, big-endian
        b"\xce\xfa\xed\xfe",  # 32-bit, little-endian
        b"\xcf\xfa\xed\xfe",  # 64-bit, little-endian
    }
)
_MACH_O_FAT = b"\xca\xfe\xba\xbe"  # also the start of every Java .class file
_MACH_O_FAT_64 = b"\xca\xfe\xba\xbf"
# A multi-architecture Mach-O counts its architectures next (a handful); a Java class file has
# its version there instead, never below 45 (Java 1.1). `file` tells them apart the same way.
_FIRST_JAVA_CLASS_VERSION = 45


class ExecutableKind(StrEnum):
    PE = "pe"  # Windows .exe, .dll, .sys, ...
    ELF = "elf"  # Linux and other Unix programs
    MACH_O = "mach-o"  # macOS programs


def executable_kind(read: Callable[[int, int], bytes]) -> ExecutableKind | None:
    """The kind of program the content is, or None if it isn't one.

    `read(offset, length)` returns up to `length` bytes from `offset`; fewer at the end of the
    content, and none past it.
    """
    header = read(0, HEADER_BYTES)
    if header[:4] == b"\x7fELF":
        return ExecutableKind.ELF
    if header[:4] in _MACH_O_THIN or header[:4] == _MACH_O_FAT_64:
        return ExecutableKind.MACH_O
    if header[:4] == _MACH_O_FAT and len(header) >= 8:
        architectures = int.from_bytes(header[4:8], "big")
        if 0 < architectures < _FIRST_JAVA_CLASS_VERSION:
            return ExecutableKind.MACH_O
    # "MZ" alone is also how some text starts; Windows only runs it with a PE header.
    if header[:2] == b"MZ" and len(header) >= _PE_OFFSET_FIELD.stop:
        offset = int.from_bytes(header[_PE_OFFSET_FIELD], "little")
        if offset > 0 and read(offset, 4) == b"PE\x00\x00":
            return ExecutableKind.PE
    return None
