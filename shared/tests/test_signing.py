import os
import shutil
import sys
from pathlib import Path

import pytest

from dropshare_shared.signing import (
    ExpectedSigner,
    RevocationUnavailableError,
    SignatureError,
    Signer,
    UnavailableVerifier,
    check_installer,
    normalize_thumbprint,
    system_verifier,
)

CERT = "ab" * 32
OTHER_CERT = "cd" * 32
EXPECTED = ExpectedSigner.of("StateLife", [CERT])


class _Signed:
    def __init__(self, name: str = "StateLife", cert: str = CERT, version: str = "1.2.0") -> None:
        self._signer = Signer(name, cert)
        self._version = version

    def signer(self, path: Path) -> Signer:
        return self._signer

    def file_version(self, path: Path) -> str:
        return self._version


def test_the_expected_name_certificate_and_version_pass(tmp_path: Path) -> None:
    check_installer(_Signed(), tmp_path / "setup.exe", EXPECTED, "1.2.0")


@pytest.mark.parametrize(
    ("verifier", "expected"),
    [
        (_Signed(name="StateLife Ltd"), EXPECTED),
        (_Signed(cert=OTHER_CERT), EXPECTED),  # same name, another certificate
        (_Signed(version="1.1.0"), EXPECTED),  # an older installer passed off as 1.2.0
        (_Signed(version="1.2.0.1"), EXPECTED),
        (_Signed(), ExpectedSigner.of("", [CERT])),
        (_Signed(), ExpectedSigner.of("StateLife", [])),
    ],
)
def test_anything_else_is_refused(
    tmp_path: Path, verifier: _Signed, expected: ExpectedSigner
) -> None:
    with pytest.raises(SignatureError):
        check_installer(verifier, tmp_path / "setup.exe", expected, "1.2.0")


def test_a_renewed_certificate_can_be_pinned_beside_the_old_one(tmp_path: Path) -> None:
    both = ExpectedSigner.of("StateLife", [CERT, OTHER_CERT])

    check_installer(_Signed(cert=OTHER_CERT), tmp_path / "setup.exe", both, "1.2.0")


def test_thumbprints_are_accepted_however_they_are_written() -> None:
    written = ":".join(["AB"] * 32)

    assert normalize_thumbprint(written) == CERT
    assert normalize_thumbprint(f" {CERT.upper()} ") == CERT
    with pytest.raises(ValueError):
        normalize_thumbprint("abc")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can check signatures")
def test_nothing_passes_where_signatures_cant_be_checked(tmp_path: Path) -> None:
    assert isinstance(system_verifier(), UnavailableVerifier)
    with pytest.raises(SignatureError):
        check_installer(system_verifier(), tmp_path / "setup.exe", EXPECTED, "1.2.0")


# Set on the Windows machine to a file with an embedded signature (e.g. a test-signed build),
# its signer's name and its version. Many system files are catalog-signed, which this check
# doesn't cover.
_SIGNED_FILE = os.environ.get("DROPSHARE_TEST_SIGNED_FILE", "")
_SIGNER = os.environ.get("DROPSHARE_TEST_SIGNER", "")
_VERSION = os.environ.get("DROPSHARE_TEST_SIGNED_VERSION", "")
_needs_signed_file = pytest.mark.skipif(
    not (_SIGNED_FILE and _SIGNER and _VERSION),
    reason="DROPSHARE_TEST_SIGNED_FILE/_SIGNER/_SIGNED_VERSION not set",
)


@pytest.mark.windows_only
@_needs_signed_file
def test_a_signed_file_names_its_signer_certificate_and_version() -> None:
    verifier = system_verifier()
    signer = verifier.signer(Path(_SIGNED_FILE))

    assert signer.name == _SIGNER
    assert len(signer.sha256) == 64
    assert verifier.file_version(Path(_SIGNED_FILE)) == _VERSION


@pytest.mark.windows_only
@_needs_signed_file
def test_a_tampered_copy_is_refused(tmp_path: Path) -> None:
    copy = tmp_path / "copy.exe"
    shutil.copyfile(_SIGNED_FILE, copy)
    with copy.open("r+b") as handle:  # flip a byte in the signed code
        handle.seek(1024)
        byte = handle.read(1)
        handle.seek(1024)
        handle.write(bytes([byte[0] ^ 0xFF]))

    with pytest.raises(SignatureError) as refused:
        system_verifier().signer(copy)
    assert not isinstance(refused.value, RevocationUnavailableError)


@pytest.mark.windows_only
def test_an_unsigned_file_is_refused(tmp_path: Path) -> None:
    unsigned = tmp_path / "unsigned.exe"
    unsigned.write_bytes(b"MZ" + bytes(510))

    with pytest.raises(SignatureError):
        system_verifier().signer(unsigned)
    with pytest.raises(SignatureError):
        system_verifier().file_version(unsigned)
