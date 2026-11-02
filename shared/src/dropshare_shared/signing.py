"""Authenticode checks for client installers (FR-UPD-02, FR-UPD-03, OQ-49).

Used by the release CLI before publishing and by the client before installing. An installer
passes only if its signature is valid and trusted, the signer has the expected name, and the
signing certificate is one of the pinned ones (by SHA-256, so another certificate with the same
name doesn't pass; several are pinned while a renewed certificate is rolled out). Its version
resource must also be the version it is published as, so a validly signed older installer
can't be passed off as a newer one.

Only Windows can check these; elsewhere every check fails, so nothing unchecked is published or
installed.
"""

import re
import sys
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SignatureError(Exception):
    """The file has no valid, trusted signature from the expected publisher."""


class RevocationUnavailableError(SignatureError):
    """Whether the certificate was revoked couldn't be checked (no network): the file may be
    fine, so it is kept and checked again later."""


@dataclass(frozen=True)
class Signer:
    name: str  # the certificate's simple display name (its CN)
    sha256: str  # SHA-256 of the signing certificate, lower-case hex


class SignatureVerifier(Protocol):
    def signer(self, path: Path) -> Signer:
        """The signer of a valid signature that chains to a trusted root; raise
        SignatureError otherwise."""

    def file_version(self, path: Path) -> str:
        """The file version from its version resource ("1.2.3", or "1.2.3.4" when the fourth
        part isn't 0); SignatureError if it has none."""


class UnavailableVerifier:
    def signer(self, path: Path) -> Signer:
        raise SignatureError("Authenticode signatures can only be checked on Windows.")

    def file_version(self, path: Path) -> str:
        raise SignatureError("Installer versions can only be read on Windows.")


def system_verifier() -> SignatureVerifier:
    if sys.platform == "win32":
        from dropshare_shared._authenticode_windows import WindowsVerifier

        return WindowsVerifier()
    return UnavailableVerifier()


def normalize_thumbprint(text: str) -> str:
    """A certificate SHA-256 as hex, however it was written (spaces, colons, upper case)."""
    value = re.sub(r"[\s:]", "", text).lower()
    if not _SHA256.fullmatch(value):
        raise ValueError(f"Not a certificate SHA-256 (64 hex characters): {text!r}")
    return value


@dataclass(frozen=True)
class ExpectedSigner:
    """Who must have signed an installer: the certificate's name and the SHA-256 of one of the
    pinned certificates."""

    name: str
    certificates: frozenset[str]

    @classmethod
    def of(cls, name: str, certificates: Collection[str]) -> "ExpectedSigner":
        return cls(name, frozenset(normalize_thumbprint(cert) for cert in certificates))

    @property
    def configured(self) -> bool:
        return bool(self.name and self.certificates)


def check_installer(
    verifier: SignatureVerifier, path: Path, expected: ExpectedSigner, version: str
) -> Signer:
    """Who signed `path`; SignatureError unless signed as `expected` and installer `version`."""
    if not expected.configured:
        raise SignatureError("No expected publisher and certificate are configured.")
    signer = verifier.signer(path)
    if signer.name != expected.name:
        raise SignatureError(f"Signed by {signer.name!r}, not by {expected.name!r}.")
    if signer.sha256 not in expected.certificates:
        raise SignatureError(
            f"Signed with certificate {signer.sha256}, which isn't one of the pinned ones."
        )
    found = verifier.file_version(path)
    if found != version:
        raise SignatureError(f"The installer is version {found}, not {version}.")
    return signer
