"""Exceptions the sync engine uses to steer itself. None of them means data was lost."""


class SyncStoppedError(Exception):
    """Stop or pause was requested; the current step was abandoned at a safe point."""


class SyncRootMissingError(Exception):
    """The sync folder is gone (deleted, or its drive is missing). Never read as "the user
    deleted everything" (windows-fs.md)."""


class FileChangingError(Exception):
    """The file changed while it was being read: it is still being written (FR-SYNC-06)."""


class LocalChangedError(Exception):
    """The item on disk is no longer what the plan was made from; plan it again."""


class RetryLaterError(Exception):
    """The step can't be done yet (e.g. its parent folder isn't synced, or the server state
    moved on); look at the item again in the next cycle. `key` is another item to look at
    too (such as the missing parent)."""

    def __init__(self, reason: str, key: str | None = None) -> None:
        super().__init__(reason)
        self.key = key


class UnsafePathError(Exception):
    """The path can't be used safely: it leads through a link or out of the sync folder, or
    two items on disk share its name. Nothing is done there; it is listed as a problem."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


class CantSyncError(Exception):
    """This item can't be synced until the user does something; list it, don't retry."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
