"""Whose files the sync folder holds, before any of them is synced.

The index records the server, user and folder it describes. A copy of the owner also lives
in the folder itself (`.dropshare-tmp/owner.json`), so it survives the index being rebuilt or
removed. A folder with files in it is only adopted when one of the two says it belongs to
this account: otherwise its files would be uploaded into the wrong account (audit B4, D3).
"""

import contextlib
import logging
import os
import tempfile
import uuid
from pathlib import Path

from pydantic import BaseModel, ValidationError

from dropshare_client.platform.base import FileAttributes
from dropshare_client.services import sync_folder
from dropshare_client.store.index import Index
from dropshare_client.store.records import Binding

logger = logging.getLogger(__name__)

_MARKER = "owner.json"


class ForeignFolderError(Exception):
    """The folder holds files this account can't be shown to own."""


class _Owner(BaseModel):
    server_url: str
    user_id: uuid.UUID


def open_index(
    index_path: Path,
    root: Path,
    server_url: str,
    user_id: uuid.UUID,
    attributes: FileAttributes,
) -> Index:
    """The index for this account and folder, ready for the engine. ForeignFolderError if the
    folder holds someone else's files, or files nobody can vouch for. Touches the disk: call
    it off the GUI thread."""
    binding = Binding(server_url, user_id, str(root))
    me = _Owner(server_url=server_url, user_id=user_id)
    index = Index(index_path)
    try:
        current = index.binding()
        if current != binding:
            owner = (
                _Owner(server_url=current.server_url, user_id=current.user_id)
                if current is not None
                else _read_owner(root)
            )
            if owner != me and sync_folder.holds_files(root):
                raise ForeignFolderError(str(root))
            index.reset(binding)
        if root.is_dir():  # a missing folder is the engine's to report (ROOT_MISSING)
            _write_owner(root, me, attributes)
    except BaseException:
        index.close()
        raise
    return index


def forget(index_path: Path) -> None:
    """Remove the index once its files are gone (sign-out with the files removed)."""
    Index.destroy(index_path)


def _read_owner(root: Path) -> _Owner | None:
    try:
        return _Owner.model_validate_json((root / sync_folder.TEMP_DIR / _MARKER).read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValidationError):
        logger.warning("Ignoring an unreadable owner marker in %s", root)
        return None


def _write_owner(root: Path, owner: _Owner, attributes: FileAttributes) -> None:
    folder = root / sync_folder.TEMP_DIR
    if not folder.is_dir():
        folder.mkdir()
        attributes.hide(folder)
    target = folder / _MARKER
    if _read_owner(root) == owner:
        return
    handle, temp_name = tempfile.mkstemp(dir=folder, prefix=".owner-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(owner.model_dump_json())
            out.flush()
            os.fsync(out.fileno())
        Path(temp_name).replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(temp_name).unlink()
        raise
