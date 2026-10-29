import ctypes
import errno
import os
from pathlib import Path

_RENAME_EXCL = 0x00000004  # <stdio.h>: fail with EEXIST if the target exists

_libc = ctypes.CDLL(None, use_errno=True)
_renamex_np = _libc.renamex_np
_renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
_renamex_np.restype = ctypes.c_int


def rename_no_replace(source: Path, target: Path) -> None:
    """Development-grade: macOS `rename` replaces an existing target; this refuses to."""
    if _renamex_np(os.fsencode(source), os.fsencode(target), _RENAME_EXCL) == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), str(target))
    raise OSError(code, os.strerror(code), str(source))
