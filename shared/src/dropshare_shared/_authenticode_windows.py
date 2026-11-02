"""WinVerifyTrust, the signer's name and certificate hash, and the file version, via ctypes (no
extra dependency).

The policy is WINTRUST_ACTION_GENERIC_VERIFY_V2: a valid signature over the file, a code-signing
certificate, and a chain to a trusted root. Revocation is checked for the whole chain except the
root, so a self-signed test certificate (its own root) works on the test machines; when the
revocation servers can't be reached, RevocationUnavailableError says so.

Only defined on Windows, so mypy checks it only when run there.
"""

import hashlib
import sys
from pathlib import Path

from dropshare_shared.signing import RevocationUnavailableError, SignatureError, Signer

if sys.platform == "win32":
    import ctypes
    import uuid
    from ctypes import POINTER, Structure, byref, c_void_p, wintypes

    _WTD_UI_NONE = 2
    _WTD_REVOKE_WHOLECHAIN = 1
    _WTD_CHOICE_FILE = 1
    _WTD_STATEACTION_VERIFY = 1
    _WTD_STATEACTION_CLOSE = 2
    _WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT = 0x80
    _CERT_NAME_SIMPLE_DISPLAY_TYPE = 4
    _GENERIC_VERIFY_V2 = "00aac56b-cd44-11d0-8cc2-00c04fc295ee"
    # The certificate may be fine; whether it was revoked just couldn't be found out.
    _REVOCATION_UNKNOWN = frozenset({0x800B010E, 0x80092013})  # REVOCATION_FAILURE, _OFFLINE

    class _GUID(Structure):
        _fields_ = (
            ("data1", wintypes.DWORD),
            ("data2", wintypes.WORD),
            ("data3", wintypes.WORD),
            ("data4", wintypes.BYTE * 8),
        )

    class _FileInfo(Structure):
        _fields_ = (
            ("cbStruct", wintypes.DWORD),
            ("pcwszFilePath", wintypes.LPCWSTR),
            ("hFile", wintypes.HANDLE),
            ("pgKnownSubject", c_void_p),
        )

    class _TrustData(Structure):
        _fields_ = (
            ("cbStruct", wintypes.DWORD),
            ("pPolicyCallbackData", c_void_p),
            ("pSIPClientData", c_void_p),
            ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD),
            ("dwUnionChoice", wintypes.DWORD),
            ("pFile", POINTER(_FileInfo)),
            ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", wintypes.HANDLE),
            ("pwszURLReference", wintypes.LPWSTR),
            ("dwProvFlags", wintypes.DWORD),
            ("dwUIContext", wintypes.DWORD),
            ("pSignatureSettings", c_void_p),
        )

    class _ProviderCert(Structure):  # the start of CRYPT_PROVIDER_CERT
        _fields_ = (("cbStruct", wintypes.DWORD), ("pCert", c_void_p))

    class _CertContext(Structure):  # CERT_CONTEXT
        _fields_ = (
            ("dwCertEncodingType", wintypes.DWORD),
            ("pbCertEncoded", POINTER(ctypes.c_ubyte)),
            ("cbCertEncoded", wintypes.DWORD),
            ("pCertInfo", c_void_p),
            ("hCertStore", c_void_p),
        )

    class _FixedFileInfo(Structure):  # VS_FIXEDFILEINFO
        _fields_ = tuple(
            (name, wintypes.DWORD)
            for name in (
                "dwSignature",
                "dwStrucVersion",
                "dwFileVersionMS",
                "dwFileVersionLS",
                "dwProductVersionMS",
                "dwProductVersionLS",
                "dwFileFlagsMask",
                "dwFileFlags",
                "dwFileOS",
                "dwFileType",
                "dwFileSubtype",
                "dwFileDateMS",
                "dwFileDateLS",
            )
        )

    _wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _version = ctypes.WinDLL("version", use_last_error=True)

    _wintrust.WinVerifyTrust.argtypes = (wintypes.HWND, POINTER(_GUID), POINTER(_TrustData))
    _wintrust.WinVerifyTrust.restype = wintypes.LONG
    _wintrust.WTHelperProvDataFromStateData.argtypes = (wintypes.HANDLE,)
    _wintrust.WTHelperProvDataFromStateData.restype = c_void_p
    _wintrust.WTHelperGetProvSignerFromChain.argtypes = (
        c_void_p,
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    _wintrust.WTHelperGetProvSignerFromChain.restype = c_void_p
    _wintrust.WTHelperGetProvCertFromChain.argtypes = (c_void_p, wintypes.DWORD)
    _wintrust.WTHelperGetProvCertFromChain.restype = POINTER(_ProviderCert)
    _crypt32.CertGetNameStringW.argtypes = (
        c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        c_void_p,
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    _crypt32.CertGetNameStringW.restype = wintypes.DWORD
    _version.GetFileVersionInfoSizeW.argtypes = (wintypes.LPCWSTR, POINTER(wintypes.DWORD))
    _version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    _version.GetFileVersionInfoW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        c_void_p,
    )
    _version.GetFileVersionInfoW.restype = wintypes.BOOL
    _version.VerQueryValueW.argtypes = (
        c_void_p,
        wintypes.LPCWSTR,
        POINTER(c_void_p),
        POINTER(wintypes.UINT),
    )
    _version.VerQueryValueW.restype = wintypes.BOOL

    def _guid(text: str) -> _GUID:
        return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)

    def _signer(state: wintypes.HANDLE) -> Signer:
        provider = _wintrust.WTHelperProvDataFromStateData(state)
        signer = (
            _wintrust.WTHelperGetProvSignerFromChain(provider, 0, False, 0) if provider else None
        )
        cert = _wintrust.WTHelperGetProvCertFromChain(signer, 0) if signer else None
        if not cert or not cert.contents.pCert:
            raise SignatureError("The signature has no signing certificate.")
        context = ctypes.cast(cert.contents.pCert, POINTER(_CertContext)).contents
        encoded = ctypes.string_at(context.pbCertEncoded, context.cbCertEncoded)
        size = _crypt32.CertGetNameStringW(
            cert.contents.pCert, _CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, None, None, 0
        )
        buffer = ctypes.create_unicode_buffer(size)
        _crypt32.CertGetNameStringW(
            cert.contents.pCert, _CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, None, buffer, size
        )
        return Signer(name=buffer.value, sha256=hashlib.sha256(encoded).hexdigest())

    class WindowsVerifier:
        def signer(self, path: Path) -> Signer:
            file_info = _FileInfo(ctypes.sizeof(_FileInfo), str(path), None, None)
            data = _TrustData(
                cbStruct=ctypes.sizeof(_TrustData),
                dwUIChoice=_WTD_UI_NONE,
                fdwRevocationChecks=_WTD_REVOKE_WHOLECHAIN,
                dwUnionChoice=_WTD_CHOICE_FILE,
                pFile=ctypes.pointer(file_info),
                dwStateAction=_WTD_STATEACTION_VERIFY,
                dwProvFlags=_WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT,
            )
            action = _guid(_GENERIC_VERIFY_V2)
            status = _wintrust.WinVerifyTrust(None, byref(action), byref(data)) & 0xFFFFFFFF
            try:
                if status in _REVOCATION_UNKNOWN:
                    raise RevocationUnavailableError(
                        f"Couldn't check whether the certificate was revoked (0x{status:08X})."
                    )
                if status != 0:
                    raise SignatureError(f"The signature isn't valid or trusted (0x{status:08X}).")
                return _signer(data.hWVTStateData)
            finally:
                data.dwStateAction = _WTD_STATEACTION_CLOSE
                _wintrust.WinVerifyTrust(None, byref(action), byref(data))

        def file_version(self, path: Path) -> str:
            size = _version.GetFileVersionInfoSizeW(str(path), None)
            block = ctypes.create_string_buffer(size) if size else None
            if block is None or not _version.GetFileVersionInfoW(str(path), 0, size, block):
                raise SignatureError("The installer has no version information.")
            pointer = c_void_p()
            length = wintypes.UINT()
            if not _version.VerQueryValueW(block, "\\", byref(pointer), byref(length)):
                raise SignatureError("The installer has no version information.")
            info = ctypes.cast(pointer, POINTER(_FixedFileInfo)).contents
            parts = (
                info.dwFileVersionMS >> 16,
                info.dwFileVersionMS & 0xFFFF,
                info.dwFileVersionLS >> 16,
                info.dwFileVersionLS & 0xFFFF,
            )
            return ".".join(str(part) for part in (parts[:3] if parts[3] == 0 else parts))
