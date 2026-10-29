"""One running client per user and profile (FR-CLIENT-07)."""

import getpass
import hashlib
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Signal, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

_CONNECT_TIMEOUT_MS = 500
_SHOW = b"show"
_OPEN = b"open "  # followed by a link (FR-SHARE-07); the running app checks it strictly
_MAX_MESSAGE = 512  # a link is far shorter; anything longer is dropped


def instance_name(profile: str | None) -> str:
    # Hashed: user names may contain characters a local socket name can't.
    key = f"{getpass.getuser()}|{profile or ''}".encode()
    return f"statelife-dropshare-{hashlib.sha256(key).hexdigest()[:16]}"


class SingleInstance(QObject):
    """The first instance holds a lock file and listens; a later one asks it to show its
    window, then exits. The lock decides who is first: on Windows two processes can both
    listen on one pipe name, so the socket alone can't."""

    activated = Signal()
    link_received = Signal(str)  # a link another start of the app was given

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
        self._sockets: dict[QLocalSocket, bytes] = {}  # what each peer sent so far

    def claim(self, link: str | None = None) -> bool:
        """True if this is the only instance; False after waking the running one (and handing
        it `link`, if this start was for one)."""
        if not self._lock.tryLock(_CONNECT_TIMEOUT_MS):
            self._wake_running_instance(link)
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

    def _wake_running_instance(self, link: str | None) -> None:
        self._before_waking()
        socket = QLocalSocket(self)
        socket.connectToServer(self._name)
        if socket.waitForConnected(_CONNECT_TIMEOUT_MS):
            message = _SHOW if link is None else _OPEN + link.encode("utf-8", "replace")
            socket.write(message[:_MAX_MESSAGE] + b"\n")
            socket.waitForBytesWritten(_CONNECT_TIMEOUT_MS)
            socket.disconnectFromServer()

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._sockets[socket] = b""  # keep the wrapper alive until the peer is done
            socket.readyRead.connect(self._read)
            socket.disconnected.connect(self._finished)

    @Slot()
    def _read(self) -> None:
        socket = self.sender()
        if isinstance(socket, QLocalSocket):
            self._take(socket)

    def _take(self, socket: QLocalSocket) -> None:
        """Act on each complete line; a peer sending more than one message is cut off."""
        buffer = self._sockets.get(socket, b"") + bytes(socket.readAll().data())
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            self._message(line)
        self._sockets[socket] = buffer if len(buffer) <= _MAX_MESSAGE else b""

    def _message(self, line: bytes) -> None:
        if line == _SHOW:
            self.activated.emit()
        elif line.startswith(_OPEN) and len(line) <= _MAX_MESSAGE:
            self.activated.emit()
            self.link_received.emit(line[len(_OPEN) :].decode("utf-8", "replace"))

    @Slot()
    def _finished(self) -> None:
        socket = self.sender()
        if isinstance(socket, QLocalSocket):
            self._take(socket)
            self._sockets.pop(socket, None)
            socket.deleteLater()
