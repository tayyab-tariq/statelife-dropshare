from dropshare_server.core.config import Settings
from dropshare_server.storage.base import StorageBackend
from dropshare_server.storage.local import LocalFilesystemBackend


def create_storage_backend(settings: Settings) -> StorageBackend:
    match settings.storage_backend:
        case "local":
            return LocalFilesystemBackend(settings.storage_local_root)
