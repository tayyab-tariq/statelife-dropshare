"""One running client per user and profile (FR-CLIENT-07)."""

import getpass
import hashlib
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Signal, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

_CONNECT_TIMEOUT_MS = 500
_SHOW = b"show\n"


def instance_name(profile: str | None) -> str:
    # Hashed: user names may contain characters a local socket name can't.
    key = f"{getpass.getuser()}|{profile or ''}".encode()
    return f"statelife-dropshare-{hashlib.sha256(key).hexdigest()[:16]}"


class SingleInstance(QObject):
    """The first instance holds a lock file and listens; a later one asks it to show its
    window, then exits. The lock decides who is first: on Windows two processes can both
    listen on one pipe name, so the socket alone can't."""

    activated = Signal()

    def __init__(
        self,
        name: str,
        lock_path: Path,
        before_waking: Callable[[], None] = lambda: None,
    ) -> None:
        super().__init__()
        self._name = name
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = QLockFile(str(lock_path))
        self._lock.setStaleLockTime(0)  # stale only if the holder's process is gone
        self._before_waking = before_waking
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        self._sockets: set[QLocalSocket] = set()

    def claim(self) -> bool:
        """True if this is the only instance; False after waking the running one."""
        if not self._lock.tryLock(_CONNECT_TIMEOUT_MS):
            self._wake_running_instance()
            return False
        QLocalServer.removeServer(self._name)  # a crashed instance can leave a stale socket
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self._server.listen(self._name):
            raise RuntimeError(
                f"Couldn't start the single-instance listener: {self._server.errorString()}"
            )
        return True

    def release(self) -> None:
        self._server.close()
        self._lock.unlock()

    def _wake_running_instance(self) -> None:
        self._before_waking()
        socket = QLocalSocket(self)
        socket.connectToServer(self._name)
        if socket.waitForConnected(_CONNECT_TIMEOUT_MS):
            socket.write(_SHOW)
            socket.waitForBytesWritten(_CONNECT_TIMEOUT_MS)
            socket.disconnectFromServer()

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._sockets.add(socket)  # keep the wrapper alive until the peer is done
            socket.readyRead.connect(self._read)
            socket.disconnected.connect(self._finished)

    @Slot()
    def _read(self) -> None:
        socket = self.sender()
        if isinstance(socket, QLocalSocket) and bytes(socket.readAll().data()).startswith(
            _SHOW.strip()
        ):
            self.activated.emit()

    @Slot()
    def _finished(self) -> None:
        socket = self.sender()
        if isinstance(socket, QLocalSocket):
            self._read()
            self._sockets.discard(socket)
            socket.deleteLater()
