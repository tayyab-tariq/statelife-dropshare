"""Client updates (FR-UPD-03, FR-UPD-04, OQ-49). No Qt: everything here runs off the UI thread.

Finds the release offered to this user, downloads its installer into the app-data `updates`
folder (resuming a partial download), and checks it: the SHA-256 the server published, an
Authenticode signature by the publisher and a certificate this build pins, and the installer's
own version being the release's (so an older signed installer can't be passed off as newer).
All is checked again just before the installer runs, since the file sat on disk in between. A
file that fails is deleted; one whose certificate couldn't be checked for revocation (offline)
is kept for later.

"Install on next restart" is remembered in `pending.json` beside the installer, so the next
start can install before syncing begins.
"""

import contextlib
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.errors import (
    ApiError,
    ContentMismatchError,
    RemoteContentChangedError,
)
from dropshare_client.services.files import (
    Progress,
    download_verified,
    file_sha256,
    no_progress,
)
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.releases import ReleaseOut, ReleasePlatform
from dropshare_shared.signing import (
    ExpectedSigner,
    RevocationUnavailableError,
    SignatureError,
    SignatureVerifier,
    check_installer,
)
from dropshare_shared.version import parse_version

logger = logging.getLogger(__name__)

PLATFORM = ReleasePlatform.WINDOWS_X64
_PENDING = "pending.json"
# Only files named like this are ever deleted from the updates folder.
_OURS = re.compile(r"^DropShare-\d+\.\d+\.\d+-setup\.exe(\.part)?$")


class UpdateVerificationError(Exception):
    """The installer didn't match its checksum, signature or version; it was deleted."""


class UpdateDeferredError(Exception):
    """The installer couldn't be fully checked now (revocation servers unreachable); it was
    kept, and is checked again later."""


@dataclass(frozen=True)
class ReadyUpdate:
    release: ReleaseOut
    installer: Path


class _Pending(BaseModel):
    release: ReleaseOut


class Updater:
    def __init__(
        self,
        api: ApiClient,
        current_version: str,
        folder: Path,
        verifier: SignatureVerifier,
        expected: ExpectedSigner,
    ) -> None:
        self._api = api
        self._current = parse_version(current_version)
        self._folder = folder
        self._verifier = verifier
        self._expected = expected

    @property
    def can_install(self) -> bool:
        """Only signed builds, which pin their publisher's certificate, install updates: never
        from source or an unsigned test build."""
        return self._expected.configured

    def newer_release(self) -> ReleaseOut | None:
        """The release offered to this user, if it is newer than this client."""
        try:
            release = self._api.data(
                "GET",
                "/api/v1/client/releases/latest",
                ReleaseOut,
                params={"platform": PLATFORM.value},
            )
        except ApiError as exc:
            if exc.code == ErrorCode.RELEASE_NOT_FOUND:
                return None
            raise
        return release if parse_version(release.version) > self._current else None

    def download(
        self, release: ReleaseOut, stop: threading.Event, progress: Progress = no_progress
    ) -> ReadyUpdate:
        """Download (or finish downloading) the installer and verify it. Raises
        UpdateVerificationError, OfflineError, TransferCancelledError (stop) or ApiError."""
        self._folder.mkdir(parents=True, exist_ok=True)
        installer = self._folder / f"DropShare-{release.version}-setup.exe"
        part = installer.with_name(installer.name + ".part")
        self._remove_installers(keep={installer.name, part.name})
        if not installer.exists():
            try:
                download_verified(
                    self._api,
                    f"/api/v1/client/releases/{release.id}/installer",
                    release.sha256,
                    release.size_bytes,
                    part,
                    stop,
                    progress,
                )
            except (ContentMismatchError, RemoteContentChangedError) as exc:
                part.unlink(missing_ok=True)
                raise UpdateVerificationError("The download didn't match its checksum.") from exc
            part.replace(installer)
        update = ReadyUpdate(release, installer)
        self.verify(update)
        return update

    def verify(self, update: ReadyUpdate) -> None:
        """Checksum, signature and version, right before installing too. Deletes a file that
        fails; UpdateDeferredError keeps one that can't be fully checked now."""
        try:
            if parse_version(update.release.version) <= self._current:
                raise UpdateVerificationError("The installer isn't newer than this version.")
            if update.installer.stat().st_size != update.release.size_bytes or (
                file_sha256(update.installer, threading.Event()) != update.release.sha256
            ):
                raise UpdateVerificationError("The installer doesn't match its checksum.")
            check_installer(
                self._verifier, update.installer, self._expected, update.release.version
            )
        except RevocationUnavailableError as exc:
            raise UpdateDeferredError(str(exc)) from exc
        except SignatureError as exc:
            self._discard(update)
            raise UpdateVerificationError(f"The installer's signature isn't valid: {exc}") from exc
        except (UpdateVerificationError, FileNotFoundError):
            self._discard(update)
            raise
        logger.info("Update %s verified", update.release.version)

    def remember(self, update: ReadyUpdate) -> None:
        """Install on the next start (FR-UPD-03)."""
        text = _Pending(release=update.release).model_dump_json()
        handle, temp_name = tempfile.mkstemp(dir=self._folder, prefix=".pending-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(text)
                out.flush()
                os.fsync(out.fileno())
            Path(temp_name).replace(self._folder / _PENDING)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def pending(self) -> ReadyUpdate | None:
        """The update remembered for this start, if it is still to be installed. Once this
        version (or a newer one) runs, the remembered update and its files are removed."""
        path = self._folder / _PENDING
        try:
            release = _Pending.model_validate_json(path.read_bytes()).release
        except FileNotFoundError:
            return None
        except (OSError, ValidationError):
            logger.warning("Ignoring an unreadable pending update", exc_info=True)
            self.forget()
            return None
        update = ReadyUpdate(release, self._folder / f"DropShare-{release.version}-setup.exe")
        if parse_version(release.version) <= self._current or not update.installer.exists():
            self.forget()
            self._remove_installers(keep=set())
            return None
        return update

    def forget(self) -> None:
        """Nothing to install at the next start."""
        (self._folder / _PENDING).unlink(missing_ok=True)

    def _discard(self, update: ReadyUpdate) -> None:
        update.installer.unlink(missing_ok=True)
        self.forget()

    def _remove_installers(self, keep: set[str]) -> None:
        """Older or abandoned downloads; only our own installer files, nothing else."""
        if not self._folder.is_dir():
            return
        for path in self._folder.iterdir():
            if _OURS.fullmatch(path.name) and path.name not in keep:
                with contextlib.suppress(OSError):  # in use: removed another time
                    path.unlink()
