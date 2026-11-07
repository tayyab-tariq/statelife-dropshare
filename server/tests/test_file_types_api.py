"""Banned file types on the server (FR-SAFE-02, OQ-56, OQ-57): every write that creates or names
a file is refused with 422 FILE_TYPE_NOT_ALLOWED and audited as file.type_blocked."""

import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.main import create_app
from dropshare_server.models import AuditEvent, FileVersion, Node, UploadSession
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome
from dropshare_shared.schemas.uploads import UploadStatus

type Account = dict[str, str]
type SignIn = Callable[..., Account]
type RootOf = Callable[[Account], str]
type AuditEvents = Callable[..., list[AuditEvent]]
type Json = dict[str, object]

_DEFAULT_BANNED = (
    ".exe", ".dll", ".msi", ".bat", ".cmd", ".com", ".scr", ".ps1", ".vbs",
    ".js", ".jse", ".wsf", ".hta", ".jar", ".lnk", ".reg", ".iso",
)  # fmt: skip


def _auth(user: Account) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def _ok(response: Response, status: int = 200) -> Json:
    assert response.status_code == status, response.text
    data: Json = response.json()["data"]
    return data


def _items(response: Response) -> list[Json]:
    assert response.status_code == 200, response.text
    items: list[Json] = response.json()["data"]
    return items


def _refused(response: Response) -> Json:
    """The FILE_TYPE_NOT_ALLOWED details of a refusal."""
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "FILE_TYPE_NOT_ALLOWED"
    details: Json = error["details"]
    return details


def _start(client: TestClient, user: Account, content: bytes, **target: object) -> Response:
    body = {key: str(value) if key.endswith("_id") else value for key, value in target.items()}
    response: Response = client.post(
        "/api/v1/uploads",
        json={**body, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()},
        headers=_auth(user),
    )
    return response


def _send(client: TestClient, user: Account, upload: Json, content: bytes) -> None:
    size = int(str(upload["chunk_size"]))
    for index in range(int(str(upload["chunk_count"]))):
        chunk = content[index * size : (index + 1) * size]
        sent = client.put(
            f"/api/v1/uploads/{upload['id']}/chunks/{index}",
            content=chunk,
            headers={**_auth(user), "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
        )
        assert sent.status_code == 204, sent.text


def _complete(client: TestClient, user: Account, upload: Json) -> Response:
    response: Response = client.post(
        f"/api/v1/uploads/{upload['id']}/complete", headers=_auth(user)
    )
    return response


def _upload(client: TestClient, user: Account, content: bytes, **target: object) -> Response:
    upload = _ok(_start(client, user, content, **target), 201)
    _send(client, user, upload, content)
    return _complete(client, user, upload)


def _details(event: AuditEvent) -> Json:
    details: Json = json.loads(event.details or "{}")
    return details


def _pe() -> bytes:
    dos = bytearray(b"MZ" + b"\x00" * 0x3E)
    dos[0x3C:0x40] = (0x40).to_bytes(4, "little")
    return bytes(dos) + b"PE\x00\x00\x64\x86" + b"\x00" * 18


_ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 25
_MACH_O = b"\xcf\xfa\xed\xfe\x07\x00\x00\x01" + b"\x00" * 24


def _zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


@pytest.fixture
def lenient_api(db_settings: Settings) -> Iterator[TestClient]:
    """The same server before any type was banned: makes files stored before a ban (OQ-57)."""
    settings = db_settings.model_copy(update={"banned_extensions": []})
    with TestClient(
        create_app(settings), raise_server_exceptions=False, headers={"X-Client-Version": "0.2.0"}
    ) as client:
        yield client


def _staged(storage_root: Path) -> list[Path]:
    staging = storage_root / "staging"
    return list(staging.iterdir()) if staging.is_dir() else []


def _blobs(storage_root: Path) -> list[Path]:
    blobs = storage_root / "blobs"
    return [path for path in blobs.rglob("*") if path.is_file()] if blobs.is_dir() else []


# --- Upload start ---------------------------------------------------------------------------


def test_every_banned_extension_is_refused_at_upload_start_in_any_case(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
    session_factory: sessionmaker[Session],
) -> None:
    alice = sign_in()
    names = [
        name
        for extension in _DEFAULT_BANNED
        for name in (f"setup{extension}", f"SETUP{extension.upper()}", f"a.pdf{extension.title()}")
    ]

    for name in names:
        details = _refused(_start(api, alice, b"data", parent_id=root_of(alice), name=name))
        assert details["reason"] == "banned_extension", name
        assert details["extension"] == "." + name.rsplit(".", 1)[1].lower(), name

    events = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert [event.target_name for event in events] == names
    assert {event.outcome for event in events} == {AuditOutcome.DENIED}
    assert _details(events[0]) == {"reason": "banned_extension", "extension": ".exe"}
    assert (events[0].target_type, events[0].target_id) == ("folder", root_of(alice))
    with session_factory() as session:  # nothing staged, nothing reserved against the quota
        assert session.scalars(select(UploadSession)).all() == []


def test_the_message_names_the_extension(api: TestClient, sign_in: SignIn, root_of: RootOf) -> None:
    alice = sign_in()

    response = _start(api, alice, b"x", parent_id=root_of(alice), name="Invoice.pdf.EXE")

    assert ".exe" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "name", ["report.exe.pdf", "exe.txt", "README", "notes.json", "setup.exe1", "app.jsx"]
)
def test_look_alike_names_are_allowed(
    api: TestClient, sign_in: SignIn, root_of: RootOf, name: str
) -> None:
    alice = sign_in()

    assert _ok(_upload(api, alice, b"data", parent_id=root_of(alice), name=name))["name"] == name


@pytest.mark.parametrize("name", ["setup.exe.", "setup.exe ", "setup.exe. "])
def test_names_windows_would_turn_into_a_banned_type_are_refused_as_invalid(
    api: TestClient, sign_in: SignIn, root_of: RootOf, name: str
) -> None:
    # Windows drops trailing dots and spaces; such names are invalid everywhere (NFR-COMPAT-02).
    alice = sign_in()

    response = _start(api, alice, b"data", parent_id=root_of(alice), name=name)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_an_editor_of_a_shared_folder_is_refused_too(
    api: TestClient, sign_in: SignIn, root_of: RootOf, audit_events: AuditEvents
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    folder = _ok(
        api.post(
            "/api/v1/nodes",
            json={"parent_id": root_of(alice), "name": "Team"},
            headers=_auth(alice),
        ),
        201,
    )
    bob_id = _ok(api.get("/api/v1/me", headers=_auth(bob)))["id"]
    _ok(
        api.post(
            f"/api/v1/nodes/{folder['id']}/members",
            json={"user_id": bob_id, "role": "editor"},
            headers=_auth(alice),
        ),
        201,
    )

    details = _refused(_start(api, bob, b"x", parent_id=folder["id"], name="tool.ps1"))

    assert details == {"reason": "banned_extension", "extension": ".ps1", "file_names": []}
    [event] = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert (event.actor_user_id is not None, event.target_name) == (True, "tool.ps1")
    assert str(event.actor_user_id) == bob_id


def test_a_file_stored_before_its_type_was_banned_gets_no_new_version(
    api: TestClient,
    lenient_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
) -> None:
    alice = sign_in()
    old = _ok(_upload(lenient_api, alice, b"v1", parent_id=root_of(alice), name="tool.exe"))

    response = _start(api, alice, b"v2", node_id=old["id"], base_revision=old["revision"])

    assert _refused(response)["extension"] == ".exe"
    [event] = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert (event.target_type, event.target_id) == ("node", old["id"])
    # It stays, and can still be downloaded (OQ-57).
    content = api.get(f"/api/v1/nodes/{old['id']}/content", headers=_auth(alice))
    assert content.content == b"v1"


# --- Upload completion ----------------------------------------------------------------------


def test_a_type_banned_while_uploading_is_refused_at_completion(
    api: TestClient,
    lenient_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
    session_factory: sessionmaker[Session],
    storage_root: Path,
) -> None:
    alice = sign_in()
    upload = _ok(_start(lenient_api, alice, b"data", parent_id=root_of(alice), name="a.exe"), 201)
    _send(api, alice, upload, b"data")

    assert _refused(_complete(api, alice, upload))["reason"] == "banned_extension"

    # No retry can succeed: the upload is cancelled and what it staged is gone.
    assert _complete(api, alice, upload).status_code == 404
    with session_factory() as session:
        assert session.scalars(select(UploadSession.status)).one() == UploadStatus.CANCELLED
        assert session.scalars(select(Node).where(Node.name == "a.exe")).all() == []
    assert _staged(storage_root) == []
    assert len(audit_events(AuditAction.FILE_TYPE_BLOCKED)) == 1


@pytest.mark.parametrize("name", ["invoice.pdf", "notes.txt", "report.docx"])
def test_a_renamed_program_is_refused_at_completion(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    name: str,
) -> None:
    # The test chunk size is 4 bytes, so a PE header is only complete when the whole file is.
    alice = sign_in()
    kind = "pe"

    response = _upload(api, alice, _pe(), parent_id=root_of(alice), name=name)

    assert _refused(response) == {
        "reason": "executable_content",
        "extension": None,
        "file_names": [],
    }
    with session_factory() as session:
        assert session.scalars(select(Node).where(Node.name == name)).all() == []
        assert session.scalars(select(FileVersion)).all() == []
        assert session.scalars(select(UploadSession.status)).one() == UploadStatus.CANCELLED
    assert (_staged(storage_root), _blobs(storage_root)) == ([], [])
    [event] = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert event.target_name == name
    assert _details(event) == {
        "reason": "executable_content",
        "extension": None,
        "executable": kind,
    }
    assert audit_events(AuditAction.FILE_UPLOADED) == []


@pytest.mark.parametrize(("content", "kind"), [(_ELF, "elf"), (_MACH_O, "mach-o")])
def test_a_program_is_refused_as_soon_as_its_first_chunk_arrives(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    content: bytes,
    kind: str,
) -> None:
    alice = sign_in()
    upload = _ok(_start(api, alice, content, parent_id=root_of(alice), name="photo.dat"), 201)

    first = content[:4]
    response = api.put(
        f"/api/v1/uploads/{upload['id']}/chunks/0",
        content=first,
        headers={**_auth(alice), "X-Chunk-SHA256": hashlib.sha256(first).hexdigest()},
    )

    assert _refused(response)["reason"] == "executable_content"
    # Cancelled: the rest is never accepted, and completing finds nothing to complete.
    second = content[4:8]
    rest = api.put(
        f"/api/v1/uploads/{upload['id']}/chunks/1",
        content=second,
        headers={**_auth(alice), "X-Chunk-SHA256": hashlib.sha256(second).hexdigest()},
    )
    assert rest.status_code == 404
    assert _complete(api, alice, upload).status_code == 404
    with session_factory() as session:
        assert session.scalars(select(UploadSession.status)).one() == UploadStatus.CANCELLED
        assert session.scalars(select(Node).where(Node.name == "photo.dat")).all() == []
    assert (_staged(storage_root), _blobs(storage_root)) == ([], [])
    [event] = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert (event.target_type, event.target_id, event.target_name) == (
        "upload",
        upload["id"],
        "photo.dat",
    )
    assert _details(event)["executable"] == kind


def test_a_program_as_a_new_version_leaves_the_old_version_current(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
    session_factory: sessionmaker[Session],
) -> None:
    alice = sign_in()
    old = _ok(_upload(api, alice, b"%PDF-1.7", parent_id=root_of(alice), name="invoice.pdf"))

    response = _upload(api, alice, _pe(), node_id=old["id"], base_revision=old["revision"])

    assert _refused(response)["reason"] == "executable_content"
    current = _ok(api.get(f"/api/v1/nodes/{old['id']}", headers=_auth(alice)))
    assert (current["revision"], current["content_hash"]) == (old["revision"], old["content_hash"])
    content = api.get(f"/api/v1/nodes/{old['id']}/content", headers=_auth(alice))
    assert content.content == b"%PDF-1.7"
    with session_factory() as session:
        assert len(session.scalars(select(FileVersion)).all()) == 1
    [event] = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert (event.target_type, event.target_id) == ("node", old["id"])


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("report.pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"),
        ("photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16),
        ("letter.docx", _zip()),
        ("archive.zip", _zip()),
        ("empty.txt", b""),
        ("mz.txt", b"MZ is where this note starts"),
    ],
)
def test_ordinary_content_is_accepted(
    api: TestClient, sign_in: SignIn, root_of: RootOf, name: str, content: bytes
) -> None:
    alice = sign_in()

    node = _ok(_upload(api, alice, content, parent_id=root_of(alice), name=name))

    assert (node["name"], node["size_bytes"]) == (name, len(content))


# --- Rename and move ------------------------------------------------------------------------


def _mkdir(client: TestClient, user: Account, parent_id: object, name: str) -> Json:
    body = {"parent_id": str(parent_id), "name": name}
    return _ok(client.post("/api/v1/nodes", json=body, headers=_auth(user)), 201)


def _patch(client: TestClient, user: Account, node: Json, **body: object) -> Response:
    current = _ok(client.get(f"/api/v1/nodes/{node['id']}", headers=_auth(user)))
    values = {key: str(value) if key.endswith("_id") else value for key, value in body.items()}
    response: Response = client.patch(
        f"/api/v1/nodes/{node['id']}",
        json={**values, "if_revision": current["revision"]},
        headers=_auth(user),
    )
    return response


def _delete(client: TestClient, user: Account, node: Json) -> None:
    current = _ok(client.get(f"/api/v1/nodes/{node['id']}", headers=_auth(user)))
    response = client.delete(
        f"/api/v1/nodes/{node['id']}",
        params={"if_revision": current["revision"]},
        headers=_auth(user),
    )
    assert response.status_code == 204, response.text


def _restore(client: TestClient, user: Account, node: Json) -> Response:
    response: Response = client.post(f"/api/v1/trash/{node['id']}/restore", headers=_auth(user))
    return response


def _share_editor(api: TestClient, owner: Account, folder: Json, editor: Account) -> None:
    editor_id = _ok(api.get("/api/v1/me", headers=_auth(editor)))["id"]
    _ok(
        api.post(
            f"/api/v1/nodes/{folder['id']}/members",
            json={"user_id": editor_id, "role": "editor"},
            headers=_auth(owner),
        ),
        201,
    )


def _name(api: TestClient, user: Account, node: Json) -> object:
    return _ok(api.get(f"/api/v1/nodes/{node['id']}", headers=_auth(user)))["name"]


@pytest.mark.parametrize("new_name", ["notes.EXE", "notes.txt.bat", ".exe", "run.Ps1"])
def test_renaming_a_file_to_a_banned_type_is_refused(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
    new_name: str,
) -> None:
    alice = sign_in()
    file = _ok(_upload(api, alice, b"data", parent_id=root_of(alice), name="notes.txt"))

    details = _refused(_patch(api, alice, file, name=new_name))

    assert details["reason"] == "banned_extension"
    assert _name(api, alice, file) == "notes.txt"
    [event] = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert (event.target_id, event.target_name) == (file["id"], "notes.txt")
    assert audit_events(AuditAction.NODE_RENAMED) == []


def test_an_editor_renaming_in_a_shared_folder_is_refused_too(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    team = _mkdir(api, alice, root_of(alice), "Team")
    file = _ok(_upload(api, alice, b"data", parent_id=team["id"], name="notes.txt"))
    _share_editor(api, alice, team, bob)

    assert _refused(_patch(api, bob, file, name="notes.cmd"))["extension"] == ".cmd"


def test_a_file_stored_before_its_type_was_banned_can_only_be_renamed_to_an_allowed_type(
    api: TestClient, lenient_api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    root = root_of(alice)
    other = _mkdir(api, alice, root, "Other")
    tool = _ok(_upload(lenient_api, alice, b"MZ", parent_id=root, name="tool.exe"))

    assert _refused(_patch(api, alice, tool, parent_id=other["id"]))["extension"] == ".exe"
    assert _refused(_patch(api, alice, tool, name="tool2.exe"))["extension"] == ".exe"

    renamed = _ok(_patch(api, alice, tool, name="tool.txt"))  # the way out
    assert renamed["name"] == "tool.txt"
    assert _ok(_patch(api, alice, tool, parent_id=other["id"]))["parent_id"] == other["id"]


def test_moving_a_banned_file_into_a_shared_folder_is_refused(
    api: TestClient, lenient_api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    root = root_of(alice)
    team = _mkdir(api, alice, root, "Team")
    _share_editor(api, alice, team, bob)
    tool = _ok(_upload(lenient_api, alice, b"MZ", parent_id=root, name="tool.exe"))

    assert _refused(_patch(api, alice, tool, parent_id=team["id"]))["reason"] == "banned_extension"
    assert _ok(api.get(f"/api/v1/nodes/{tool['id']}", headers=_auth(alice)))["parent_id"] == root


def test_folder_names_are_not_file_types(api: TestClient, sign_in: SignIn, root_of: RootOf) -> None:
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "Installers.exe")

    assert _ok(_patch(api, alice, folder, name="Setup.msi"))["name"] == "Setup.msi"


@pytest.mark.parametrize("change", ["rename", "move", "move_to_shared"])
def test_a_folder_holding_a_banned_file_can_t_be_moved_or_renamed(
    api: TestClient,
    lenient_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
    change: str,
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    root = root_of(alice)
    projects = _mkdir(api, alice, root, "Projects")
    deep = _mkdir(api, alice, _mkdir(api, alice, projects["id"], "a")["id"], "b")
    _upload(api, alice, b"ok", parent_id=deep["id"], name="readme.txt")
    tool = _ok(_upload(lenient_api, alice, b"MZ", parent_id=deep["id"], name="Tool.EXE"))
    team = _mkdir(api, alice, root, "Team")
    _share_editor(api, alice, team, bob)
    bodies: dict[str, dict[str, object]] = {
        "rename": {"name": "Work"},
        "move": {"parent_id": _mkdir(api, alice, root, "Archive")["id"]},
        "move_to_shared": {"parent_id": team["id"]},
    }
    body = bodies[change]

    details = _refused(_patch(api, alice, projects, **body))

    assert details == {
        "reason": "contains_banned_file",
        "extension": None,
        "file_names": ["Tool.EXE"],
    }
    assert _name(api, alice, projects) == "Projects"
    [event] = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert (event.target_id, event.target_name) == (projects["id"], "Projects")
    assert _details(event) == {
        "reason": "contains_banned_file",
        "extension": None,
        "file_name": "Tool.EXE",
        "files_shown": 1,
    }
    # Renaming the file to an allowed type lets the folder move.
    _ok(_patch(api, alice, tool, name="Tool.txt"))
    assert _patch(api, alice, projects, **body).status_code == 200


def test_a_folder_refusal_lists_at_most_ten_files_and_the_message_names_one(
    api: TestClient, lenient_api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "Tools")
    for index in range(12):
        _upload(lenient_api, alice, b"x", parent_id=folder["id"], name=f"t{index:02}.bat")

    response = _patch(api, alice, folder, name="Old tools")

    details = _refused(response)
    assert details["file_names"] == [f"t{index:02}.bat" for index in range(10)]
    assert "t00.bat" in response.json()["error"]["message"]


def test_a_banned_file_in_the_trash_does_not_hold_its_folder(
    api: TestClient, lenient_api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "Projects")
    tool = _ok(_upload(lenient_api, alice, b"MZ", parent_id=folder["id"], name="tool.exe"))
    _delete(api, alice, tool)

    assert _ok(_patch(api, alice, folder, name="Work"))["name"] == "Work"


def test_ban_list_entries_with_like_wildcards_match_literally(
    db_settings: Settings, lenient_api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    folder = _mkdir(lenient_api, alice, root_of(alice), "Data")
    _upload(lenient_api, alice, b"x", parent_id=folder["id"], name="f.abc")  # ".a_c" as a pattern
    _upload(lenient_api, alice, b"x", parent_id=folder["id"], name="g.bxyz")  # ".b%" as a pattern
    settings = db_settings.model_copy(update={"banned_extensions": [".a_c", ".b%"]})
    with TestClient(
        create_app(settings), raise_server_exceptions=False, headers={"X-Client-Version": "0.2.0"}
    ) as strict:
        assert _patch(strict, alice, folder, name="Data 2").status_code == 200
        _upload(lenient_api, alice, b"x", parent_id=folder["id"], name="h.A_C")

        assert _refused(_patch(strict, alice, folder, name="Data 3"))["file_names"] == ["h.A_C"]


def test_a_banned_file_below_more_than_a_thousand_sibling_folders_is_found(
    api: TestClient, lenient_api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    # One level holding more folders than Oracle allows in an IN list (ORA-01795).
    alice = sign_in()
    big = _mkdir(api, alice, root_of(alice), "Big")
    folders = [_mkdir(api, alice, big["id"], f"f{index:04}") for index in range(1001)]
    _upload(lenient_api, alice, b"MZ", parent_id=folders[-1]["id"], name="tool.exe")

    assert _refused(_patch(api, alice, big, name="Bigger"))["file_names"] == ["tool.exe"]


# --- Restore from the trash -----------------------------------------------------------------


def test_restoring_a_banned_file_is_refused(
    api: TestClient,
    lenient_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
) -> None:
    alice = sign_in()
    tool = _ok(_upload(lenient_api, alice, b"MZ", parent_id=root_of(alice), name="tool.exe"))
    _delete(api, alice, tool)

    details = _refused(_restore(api, alice, tool))

    assert (details["reason"], details["extension"]) == ("banned_extension", ".exe")
    trash = _items(api.get("/api/v1/trash", headers=_auth(alice)))
    assert [item["id"] for item in trash] == [tool["id"]]
    [event] = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert (event.target_id, event.target_name) == (tool["id"], "tool.exe")
    assert audit_events(AuditAction.NODE_RESTORED) == []


def test_restoring_a_folder_leaves_its_banned_files_in_the_trash(
    api: TestClient,
    lenient_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
) -> None:
    # OQ-57 (9): everything else comes back; nothing allowed is ever held in the trash.
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "Projects")
    inner = _mkdir(api, alice, folder["id"], "inner")
    readme = _ok(_upload(api, alice, b"ok", parent_id=inner["id"], name="readme.txt"))
    setup = _ok(_upload(lenient_api, alice, b"MZ", parent_id=inner["id"], name="setup.msi"))
    _delete(api, alice, folder)

    restored = _ok(_restore(api, alice, folder))

    assert restored["name"] == "Projects"
    children = _items(api.get(f"/api/v1/nodes/{inner['id']}/children", headers=_auth(alice)))
    assert [child["id"] for child in children] == [readme["id"]]
    trash = _items(api.get("/api/v1/trash", headers=_auth(alice)))
    assert [item["id"] for item in trash] == [setup["id"]]  # its own trash item now
    [restored_event] = audit_events(AuditAction.NODE_RESTORED)
    assert _details(restored_event)["left_in_trash"] == 1
    [blocked] = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert (blocked.target_id, _details(blocked)["file_name"]) == (folder["id"], "setup.msi")
    # It can't come back on its own, but can be deleted for good.
    assert _refused(_restore(api, alice, setup))["extension"] == ".msi"
    purged = api.delete(f"/api/v1/trash/{setup['id']}", headers=_auth(alice))
    assert purged.status_code == 204, purged.text
    assert _items(api.get("/api/v1/trash", headers=_auth(alice))) == []


def test_restoring_an_allowed_file_still_works(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    file = _ok(_upload(api, alice, b"data", parent_id=root_of(alice), name="notes.txt"))
    _delete(api, alice, file)

    assert _ok(_restore(api, alice, file))["name"] == "notes.txt"


# --- The list for clients (FR-SAFE-03) ------------------------------------------------------


def test_signed_in_clients_get_the_ban_list_and_size_limit(
    api: TestClient, sign_in: SignIn, db_settings: Settings
) -> None:
    alice = sign_in()

    policy = _ok(api.get("/api/v1/file-policy", headers=_auth(alice)))

    assert policy == {
        "banned_extensions": sorted(_DEFAULT_BANNED),
        "max_file_size_bytes": db_settings.max_file_size_bytes,
    }


def test_the_file_policy_needs_a_signed_in_user(api: TestClient) -> None:
    response = api.get("/api/v1/file-policy")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


# --- Sharing (OQ-57 (10)) -------------------------------------------------------------------


def _add_member(api: TestClient, owner: Account, node: Json, other: Account) -> Response:
    other_id = _ok(api.get("/api/v1/me", headers=_auth(other)))["id"]
    response: Response = api.post(
        f"/api/v1/nodes/{node['id']}/members",
        json={"user_id": other_id, "role": "viewer"},
        headers=_auth(owner),
    )
    return response


def test_sharing_a_banned_file_is_refused(
    api: TestClient,
    lenient_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    audit_events: AuditEvents,
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    tool = _ok(_upload(lenient_api, alice, b"MZ", parent_id=root_of(alice), name="tool.exe"))

    assert _refused(_add_member(api, alice, tool, bob))["extension"] == ".exe"
    assert _items(api.get("/api/v1/shared-with-me", headers=_auth(bob))) == []
    [event] = audit_events(AuditAction.FILE_TYPE_BLOCKED)
    assert event.target_id == tool["id"]
    assert audit_events(AuditAction.SHARE_CREATED) == []


def test_sharing_a_folder_holding_a_banned_file_is_refused(
    api: TestClient, lenient_api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    folder = _mkdir(api, alice, root_of(alice), "Projects")
    _upload(lenient_api, alice, b"MZ", parent_id=folder["id"], name="tool.exe")

    details = _refused(_add_member(api, alice, folder, bob))

    assert details["file_names"] == ["tool.exe"]
    namespaces = _items(api.get("/api/v1/namespaces", headers=_auth(alice)))
    assert [namespace["kind"] for namespace in namespaces] == ["personal"]  # not shared


def test_a_new_member_of_a_shared_folder_holding_a_banned_file_is_refused(
    api: TestClient, lenient_api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice, bob, carol = sign_in("alice"), sign_in("bob"), sign_in("carol")
    team = _mkdir(api, alice, root_of(alice), "Team")
    _ok(_add_member(api, alice, team, bob), 201)
    _upload(lenient_api, alice, b"MZ", parent_id=team["id"], name="tool.exe")  # list changed

    assert _refused(_add_member(api, alice, team, carol))["file_names"] == ["tool.exe"]


def test_sharing_an_allowed_folder_still_works(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    folder = _mkdir(api, alice, root_of(alice), "Projects")
    _upload(api, alice, b"ok", parent_id=folder["id"], name="notes.txt")

    _ok(_add_member(api, alice, folder, bob), 201)


# --- Connection use -------------------------------------------------------------------------


def test_refusals_need_only_one_database_connection_at_a_time(
    db_settings: Settings,
    sign_in: SignIn,
    root_of: RootOf,
    lenient_api: TestClient,
    audit_events: AuditEvents,
) -> None:
    """A refusal audits after the request has given its connection back: holding one while
    taking another let a burst of refusals take the whole pool (audit 2026-11-05-0304)."""
    alice = sign_in()
    root = root_of(alice)
    notes = _ok(_upload(lenient_api, alice, b"data", parent_id=root, name="notes.txt"))
    tool = _ok(_upload(lenient_api, alice, b"MZ", parent_id=root, name="tool.exe"))
    folder = _mkdir(lenient_api, alice, root, "Tools")
    _upload(lenient_api, alice, b"MZ", parent_id=folder["id"], name="setup.msi")
    _delete(lenient_api, alice, tool)
    settings = db_settings.model_copy(update={"db_pool_size": 1, "db_max_overflow": 0})
    with TestClient(
        create_app(settings), raise_server_exceptions=False, headers={"X-Client-Version": "0.2.0"}
    ) as single:
        _refused(_start(single, alice, b"x", parent_id=root, name="a.exe"))
        _refused(_patch(single, alice, notes, name="notes.bat"))
        _refused(_patch(single, alice, folder, name="Old tools"))
        _refused(_restore(single, alice, tool))

    # Holding the request's connection, each audit write timed out and was lost.
    assert len(audit_events(AuditAction.FILE_TYPE_BLOCKED)) == 4
