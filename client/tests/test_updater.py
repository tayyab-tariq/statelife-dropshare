"""Finding, downloading and verifying updates (FR-UPD-03, OQ-49) against a fake server."""

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.tokens import TokenStore
from dropshare_client.services.updater import UpdateDeferredError, Updater, UpdateVerificationError
from dropshare_shared.schemas.auth import TokenPairOut
from dropshare_shared.schemas.releases import ReleaseOut
from dropshare_shared.signing import (
    ExpectedSigner,
    RevocationUnavailableError,
    SignatureError,
    Signer,
)

PUBLISHER = "StateLife Test"
INSTALLER = b"MZ" + bytes(range(256)) * 40


class _Credentials:
    def __init__(self) -> None:
        self.secret: str | None = None

    def get(self) -> str | None:
        return self.secret

    def set(self, secret: str) -> None:
        self.secret = secret

    def delete(self) -> None:
        self.secret = None


CERT = "ab" * 32
EXPECTED = ExpectedSigner.of(PUBLISHER, [CERT])


class _Verifier:
    """Signed by PUBLISHER with CERT; the installer's version is the one in its file name
    unless `version` says otherwise. `offline` makes the revocation check unavailable."""

    def __init__(self) -> None:
        self.signer_name: str | None = PUBLISHER
        self.cert = CERT
        self.version: str | None = None
        self.offline = False
        self.checked: list[Path] = []

    def signer(self, path: Path) -> Signer:
        self.checked.append(path)
        if self.offline:
            raise RevocationUnavailableError("offline")
        if self.signer_name is None:
            raise SignatureError("No signature was found.")
        return Signer(self.signer_name, self.cert)

    def file_version(self, path: Path) -> str:
        return self.version or path.name.removeprefix("DropShare-").removesuffix("-setup.exe")


class FakeReleases:
    """GET /client/releases/latest and the installer, with one-range support like the server."""

    def __init__(self) -> None:
        self.release: dict[str, object] | None = None
        self.content = INSTALLER
        self.requests: list[httpx.Request] = []

    def publish(self, version: str, content: bytes = INSTALLER) -> ReleaseOut:
        self.content = content
        self.release = {
            "id": str(uuid.uuid4()),
            "version": version,
            "channel": "stable",
            "min_supported_version": "1.0.0",
            "notes": "Fixes",
            "size_bytes": len(INSTALLER),
            "sha256": hashlib.sha256(INSTALLER).hexdigest(),
            "published_at": datetime.now(UTC).isoformat(),
        }
        return ReleaseOut.model_validate(self.release)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/v1/client/releases/latest":
            assert request.url.params["platform"] == "windows-x64"
            if self.release is None:
                error = {"code": "RELEASE_NOT_FOUND", "message": "None", "details": None}
                return httpx.Response(404, json={"error": error})
            return httpx.Response(200, json={"data": self.release})
        assert self.release is not None
        assert request.url.path == f"/api/v1/client/releases/{self.release['id']}/installer"
        sha = self.release["sha256"]  # as the server does: the recorded checksum, not the bytes
        start = 0
        if "Range" in request.headers:
            start = int(request.headers["Range"].removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(206, content=self.content[start:], headers={"ETag": f'"{sha}"'})
        return httpx.Response(200, content=self.content, headers={"ETag": f'"{sha}"'})


@pytest.fixture
def server() -> FakeReleases:
    return FakeReleases()


@pytest.fixture
def verifier() -> _Verifier:
    return _Verifier()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return tmp_path / "updates"


def _updater(
    server: FakeReleases,
    folder: Path,
    verifier: _Verifier,
    current: str = "1.1.0",
    expected: ExpectedSigner = EXPECTED,
) -> Updater:
    tokens = TokenStore(_Credentials())
    tokens.store(
        TokenPairOut(
            access_token="access",
            refresh_token="refresh",
            expires_in=900,
            must_change_password=False,
        )
    )
    api = ApiClient(
        "https://dropshare.example.com",
        tokens,
        client_version=current,
        user_agent="test",
        transport=httpx.MockTransport(server.handle),
    )
    return Updater(api, current, folder, verifier, expected)


@pytest.mark.parametrize(
    ("published", "current", "offered"),
    [
        (None, "1.1.0", False),
        ("1.1.0", "1.1.0", False),
        ("1.0.9", "1.1.0", False),
        ("1.2.0", "1.1.0", True),
        ("1.10.0", "1.9.0", True),  # numbers, not text
    ],
)
def test_only_a_newer_release_is_offered(
    server: FakeReleases,
    folder: Path,
    verifier: _Verifier,
    published: str | None,
    current: str,
    offered: bool,
) -> None:
    if published is not None:
        server.publish(published)

    release = _updater(server, folder, verifier, current).newer_release()

    assert (release is not None) == offered


def test_a_download_is_verified_by_checksum_and_signature(
    server: FakeReleases, folder: Path, verifier: _Verifier
) -> None:
    release = server.publish("1.2.0")
    seen: list[int] = []

    update = _updater(server, folder, verifier).download(release, threading.Event(), seen.append)

    assert update.installer == folder / "DropShare-1.2.0-setup.exe"
    assert update.installer.read_bytes() == INSTALLER
    assert verifier.checked == [update.installer]
    assert seen[-1] == len(INSTALLER)
    assert sorted(p.name for p in folder.iterdir()) == ["DropShare-1.2.0-setup.exe"]


def test_an_interrupted_download_resumes(
    server: FakeReleases, folder: Path, verifier: _Verifier
) -> None:
    release = server.publish("1.2.0")
    folder.mkdir()
    (folder / "DropShare-1.2.0-setup.exe.part").write_bytes(INSTALLER[:1000])

    update = _updater(server, folder, verifier).download(release, threading.Event())

    assert update.installer.read_bytes() == INSTALLER
    (download,) = [r for r in server.requests if r.url.path.endswith("/installer")]
    assert download.headers["Range"] == "bytes=1000-"


def test_a_download_that_does_not_match_its_checksum_is_deleted(
    server: FakeReleases, folder: Path, verifier: _Verifier
) -> None:
    release = server.publish("1.2.0", content=b"MZ something else entirely")

    with pytest.raises(UpdateVerificationError):
        _updater(server, folder, verifier).download(release, threading.Event())

    assert list(folder.iterdir()) == []
    assert verifier.checked == []  # never got as far as the signature


@pytest.mark.parametrize("signer", [None, "Someone Else"])
def test_an_installer_not_signed_by_the_publisher_is_deleted(
    server: FakeReleases, folder: Path, verifier: _Verifier, signer: str | None
) -> None:
    release = server.publish("1.2.0")
    verifier.signer_name = signer

    with pytest.raises(UpdateVerificationError):
        _updater(server, folder, verifier).download(release, threading.Event())

    assert list(folder.iterdir()) == []


@pytest.mark.parametrize(
    "change",
    [
        lambda verifier: setattr(verifier, "cert", "cd" * 32),  # same name, unpinned certificate
        lambda verifier: setattr(verifier, "version", "1.0.0"),  # an older installer as 1.2.0
    ],
)
def test_an_installer_with_another_certificate_or_version_is_deleted(
    server: FakeReleases,
    folder: Path,
    verifier: _Verifier,
    change: Callable[[_Verifier], None],
) -> None:
    release = server.publish("1.2.0")
    change(verifier)

    with pytest.raises(UpdateVerificationError):
        _updater(server, folder, verifier).download(release, threading.Event())

    assert list(folder.iterdir()) == []


def test_an_installer_that_cant_be_checked_offline_is_kept(
    server: FakeReleases, folder: Path, verifier: _Verifier
) -> None:
    release = server.publish("1.2.0")
    verifier.offline = True

    with pytest.raises(UpdateDeferredError):
        _updater(server, folder, verifier).download(release, threading.Event())

    assert [p.name for p in folder.iterdir()] == ["DropShare-1.2.0-setup.exe"]


def test_a_pending_update_no_newer_than_this_version_is_refused(
    server: FakeReleases, folder: Path, verifier: _Verifier
) -> None:
    updater = _updater(server, folder, verifier)
    update = updater.download(server.publish("1.2.0"), threading.Event())

    with pytest.raises(UpdateVerificationError):
        _updater(server, folder, verifier, current="1.2.0").verify(update)


def test_a_build_without_a_publisher_installs_nothing(
    server: FakeReleases, folder: Path, verifier: _Verifier
) -> None:
    release = server.publish("1.2.0")
    updater = _updater(server, folder, verifier, expected=ExpectedSigner.of(PUBLISHER, []))

    assert not updater.can_install
    with pytest.raises(UpdateVerificationError):
        updater.download(release, threading.Event())


def test_a_file_changed_after_download_is_caught_before_installing(
    server: FakeReleases, folder: Path, verifier: _Verifier
) -> None:
    updater = _updater(server, folder, verifier)
    update = updater.download(server.publish("1.2.0"), threading.Event())
    updater.remember(update)
    update.installer.write_bytes(INSTALLER[:-1] + b"!")

    with pytest.raises(UpdateVerificationError):
        updater.verify(update)

    assert not update.installer.exists()
    assert updater.pending() is None


def test_install_on_next_restart_is_remembered_until_the_new_version_runs(
    server: FakeReleases, folder: Path, verifier: _Verifier
) -> None:
    updater = _updater(server, folder, verifier)
    update = updater.download(server.publish("1.2.0"), threading.Event())
    updater.remember(update)

    restarted = _updater(server, folder, verifier).pending()
    upgraded = _updater(server, folder, verifier, current="1.2.0").pending()

    assert restarted == update
    assert upgraded is None
    assert list(folder.iterdir()) == []  # the installer and the note are cleaned up


def test_an_unreadable_pending_note_is_ignored(
    server: FakeReleases, folder: Path, verifier: _Verifier
) -> None:
    folder.mkdir()
    (folder / "pending.json").write_text(json.dumps({"release": {"version": "x"}}))

    assert _updater(server, folder, verifier).pending() is None
    assert not (folder / "pending.json").exists()


def test_only_our_own_old_installers_are_removed(
    server: FakeReleases, folder: Path, verifier: _Verifier
) -> None:
    folder.mkdir()
    (folder / "DropShare-1.1.5-setup.exe").write_bytes(b"old")
    (folder / "DropShare-1.1.6-setup.exe.part").write_bytes(b"old")
    (folder / "notes.txt").write_text("someone else's")

    _updater(server, folder, verifier).download(server.publish("1.2.0"), threading.Event())

    assert sorted(p.name for p in folder.iterdir()) == ["DropShare-1.2.0-setup.exe", "notes.txt"]
