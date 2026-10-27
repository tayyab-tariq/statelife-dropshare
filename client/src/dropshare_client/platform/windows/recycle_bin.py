"""Recycle Bin via IFileOperation, which never deletes permanently (FR-SYNC-09, audit D1).

SHFileOperation can't be made safe: with FOF_NOCONFIRMATION it silently deletes anything that
can't be recycled (too big for the bin, bin turned off), and FOF_WANTNUKEWARNING only adds a
"delete permanently?" prompt. IFileOperation tells a progress sink, before each item, whether
it will be recycled (TSF_DELETE_RECYCLE_IF_POSSIBLE in PreDeleteItem); the sink aborts the
operation otherwise, so files are either recycled or left where they are.

The COM plumbing below is only defined on Windows, so mypy checks it only when run there.
"""

import os
import sys
from pathlib import Path

TSF_DELETE_RECYCLE_IF_POSSIBLE = 0x00000080


def will_recycle(pre_delete_flags: int) -> bool:
    """True if the shell will recycle an item, from the flags PreDeleteItem receives."""
    return bool(pre_delete_flags & TSF_DELETE_RECYCLE_IF_POSSIBLE)


if sys.platform == "win32":
    import ctypes
    import uuid
    from collections.abc import Callable
    from ctypes import POINTER, byref, c_long, c_uint, c_ulong, c_void_p, c_wchar_p, wintypes

    _S_OK = 0
    _E_NOINTERFACE = 0x80004002 - 2**32
    _E_ABORT = 0x80004004 - 2**32
    _COINIT_APARTMENTTHREADED = 0x2
    _CLSCTX_INPROC_SERVER = 0x1
    _FOF_SILENT = 0x0004
    _FOF_NOCONFIRMATION = 0x0010
    _FOF_ALLOWUNDO = 0x0040
    _FOF_NOERRORUI = 0x0400
    _FOFX_RECYCLEONDELETE = 0x00080000
    _FOFX_EARLYFAILURE = 0x00100000
    _FLAGS = (
        _FOF_ALLOWUNDO
        | _FOF_NOCONFIRMATION
        | _FOF_SILENT
        | _FOF_NOERRORUI
        | _FOFX_RECYCLEONDELETE
        | _FOFX_EARLYFAILURE
    )

    class _GUID(ctypes.Structure):
        _fields_ = (
            ("data1", c_ulong),
            ("data2", ctypes.c_ushort),
            ("data3", ctypes.c_ushort),
            ("data4", ctypes.c_ubyte * 8),
        )

    def _guid(text: str) -> _GUID:
        return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)

    _CLSID_FILE_OPERATION = _guid("3AD05575-8857-4850-9277-11B85BDB8E09")
    _IID_FILE_OPERATION = _guid("947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8")
    _IID_SHELL_ITEM = _guid("43826D1E-E718-42EE-BC55-A1E261C37BFE")
    _IID_PROGRESS_SINK = _guid("04B0F1A7-9490-44BC-96E1-4296A31252E2")
    _IID_UNKNOWN = _guid("00000000-0000-0000-C000-000000000046")

    # IFileOperation vtable slots (IUnknown takes 0-2).
    _RELEASE = 2
    _ADVISE = 3
    _UNADVISE = 4
    _SET_OPERATION_FLAGS = 5
    _DELETE_ITEM = 18
    _PERFORM_OPERATIONS = 21
    _GET_ANY_OPERATIONS_ABORTED = 22

    def _method(obj: c_void_p, slot: int, *argtypes: object) -> Callable[..., object]:
        """Call COM method `slot` of `obj`; a failing HRESULT raises OSError."""
        vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)
        function = prototype(vtable[slot])
        return lambda *args: function(obj, *args)

    def _release(obj: c_void_p) -> None:
        if obj:
            vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
            ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtable[_RELEASE])(obj)

    _Item = c_void_p
    _Str = c_wchar_p
    _Dw = wintypes.DWORD
    _Hr = c_long

    class _ProgressSink:
        """A minimal IFileOperationProgressSink that vetoes every non-recycling delete."""

        def __init__(self) -> None:
            self.refused = False
            ok = _S_OK
            proto = ctypes.WINFUNCTYPE

            def query_interface(this: int, riid: object, ppv: object) -> int:
                wanted = ctypes.cast(riid, POINTER(_GUID))[0]
                out = ctypes.cast(ppv, POINTER(c_void_p))
                if bytes(wanted) in (bytes(_IID_UNKNOWN), bytes(_IID_PROGRESS_SINK)):
                    out[0] = this
                    return ok
                out[0] = None
                return _E_NOINTERFACE

            def pre_delete_item(this: int, flags: int, item: int) -> int:
                if will_recycle(flags):
                    return ok
                self.refused = True
                return _E_ABORT

            # Every method, in vtable order, with its exact signature.
            self._callbacks = (
                proto(_Hr, c_void_p, c_void_p, c_void_p)(query_interface),
                proto(c_ulong, c_void_p)(lambda this: 1),  # AddRef: lifetime held here
                proto(c_ulong, c_void_p)(lambda this: 1),  # Release
                proto(_Hr, c_void_p)(lambda this: ok),  # StartOperations
                proto(_Hr, c_void_p, _Hr)(lambda this, hr: ok),  # FinishOperations
                proto(_Hr, c_void_p, _Dw, _Item, _Str)(lambda *a: ok),  # PreRenameItem
                proto(_Hr, c_void_p, _Dw, _Item, _Str, _Hr, _Item)(lambda *a: ok),  # PostRename
                proto(_Hr, c_void_p, _Dw, _Item, _Item, _Str)(lambda *a: ok),  # PreMoveItem
                proto(_Hr, c_void_p, _Dw, _Item, _Item, _Str, _Hr, _Item)(lambda *a: ok),
                proto(_Hr, c_void_p, _Dw, _Item, _Item, _Str)(lambda *a: ok),  # PreCopyItem
                proto(_Hr, c_void_p, _Dw, _Item, _Item, _Str, _Hr, _Item)(lambda *a: ok),
                proto(_Hr, c_void_p, _Dw, _Item)(pre_delete_item),
                proto(_Hr, c_void_p, _Dw, _Item, _Hr, _Item)(lambda *a: ok),  # PostDeleteItem
                proto(_Hr, c_void_p, _Dw, _Item, _Str)(lambda *a: ok),  # PreNewItem
                proto(_Hr, c_void_p, _Dw, _Item, _Str, _Str, _Dw, _Hr, _Item)(lambda *a: ok),
                proto(_Hr, c_void_p, c_uint, c_uint)(lambda *a: ok),  # UpdateProgress
                proto(_Hr, c_void_p)(lambda this: ok),  # ResetTimer
                proto(_Hr, c_void_p)(lambda this: ok),  # PauseTimer
                proto(_Hr, c_void_p)(lambda this: ok),  # ResumeTimer
            )
            self._vtable = (c_void_p * len(self._callbacks))(
                *(ctypes.cast(callback, c_void_p) for callback in self._callbacks)
            )
            self._object = (c_void_p * 1)(ctypes.addressof(self._vtable))

        @property
        def pointer(self) -> int:
            return ctypes.addressof(self._object)

    def _recycle(target: str) -> None:
        ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32
        initialised = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        if initialised < 0:
            raise OSError(f"COM couldn't start (HRESULT {initialised & 0xFFFFFFFF:#x})")
        operation, item = c_void_p(), c_void_p()
        try:
            result = ole32.CoCreateInstance(
                byref(_CLSID_FILE_OPERATION),
                None,
                _CLSCTX_INPROC_SERVER,
                byref(_IID_FILE_OPERATION),
                byref(operation),
            )
            if result < 0:
                raise OSError(f"IFileOperation unavailable (HRESULT {result & 0xFFFFFFFF:#x})")
            result = shell32.SHCreateItemFromParsingName(
                c_wchar_p(target), None, byref(_IID_SHELL_ITEM), byref(item)
            )
            if result < 0:
                raise OSError(f"Can't open {target} (HRESULT {result & 0xFFFFFFFF:#x})")
            sink, cookie = _ProgressSink(), wintypes.DWORD()
            _method(operation, _SET_OPERATION_FLAGS, wintypes.DWORD)(_FLAGS)
            _method(operation, _ADVISE, c_void_p, POINTER(wintypes.DWORD))(
                sink.pointer, byref(cookie)
            )
            try:
                _method(operation, _DELETE_ITEM, c_void_p, c_void_p)(item, None)
                try:
                    _method(operation, _PERFORM_OPERATIONS)()
                except OSError:
                    if not sink.refused:
                        raise
            finally:
                _method(operation, _UNADVISE, wintypes.DWORD)(cookie.value)
            aborted = wintypes.BOOL()
            _method(operation, _GET_ANY_OPERATIONS_ABORTED, POINTER(wintypes.BOOL))(byref(aborted))
            if sink.refused:
                raise OSError(f"{target} can't go to the Recycle Bin, so it was left in place")
            if aborted:
                raise OSError(f"Moving {target} to the Recycle Bin was cancelled")
        finally:
            _release(item)
            _release(operation)
            ole32.CoUninitialize()

    class WindowsRecycleBin:
        def move_to_recycle_bin(self, path: Path) -> None:
            if sys.platform != "win32":
                raise OSError("The Windows Recycle Bin is only available on Windows")
            if not path.exists():
                raise FileNotFoundError(str(path))
            # normpath, not resolve(): a junction or link is recycled itself, never its target.
            _recycle(os.path.normpath(path.absolute()))
            if path.exists():
                raise OSError(f"{path} is still there after moving it to the Recycle Bin")
