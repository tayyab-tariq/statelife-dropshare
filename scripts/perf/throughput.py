"""NFR-PERF-04: large transfers use at least 80% of the available bandwidth on a stable
connection.

"Available bandwidth" is measured on the same connection to the same server with the
protocol stripped to its bytes: the chunks sent back to back with their hashes worked out
beforehand (upload), and the file's content streamed and thrown away (download). The client's
own transfer, with hashing, temp files, fsync and verification, is then compared with that.

    uv run python scripts/perf/throughput.py --server http://127.0.0.1:8000 --mb 1024
"""

import argparse
import hashlib
import os
import tempfile
import threading
import time
from pathlib import Path

from perf_common import (
    add_target_arguments,
    check_target,
    ensure_users,
    record,
    say,
    server_label,
    sign_in,
)

from dropshare_client.services.files import NewFile, RemoteFiles, SourceFile
from dropshare_shared.schemas.envelope import DataResponse
from dropshare_shared.schemas.nodes import NamespaceOut
from dropshare_shared.schemas.uploads import CHUNK_SHA256_HEADER, UploadCreateIn, UploadOut

_TARGET = 0.80
_BLOCK = 8 * 1024 * 1024


def _random_file(path: Path, size: int) -> SourceFile:
    digest = hashlib.sha256()
    with path.open("wb") as out:
        left = size
        while left:
            block = os.urandom(min(_BLOCK, left))
            out.write(block)
            digest.update(block)
            left -= len(block)
    info = path.stat()
    return SourceFile(path, digest.hexdigest(), info.st_size, info.st_mtime_ns)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    add_target_arguments(parser)
    parser.add_argument("--mb", type=int, default=1024, help="file size in MiB")
    args = parser.parse_args()
    check_target(args)

    [name] = ensure_users(args.server, 1)
    signed = sign_in(args.server, name)
    api, files = signed.api, RemoteFiles(signed.api)
    root = api.envelope("GET", "/api/v1/namespaces", DataResponse[list[NamespaceOut]]).data[0]
    size = args.mb * 1024 * 1024
    stop = threading.Event()
    with tempfile.TemporaryDirectory(prefix="dropshare-perf-") as temp:
        say(f"Writing a {args.mb} MiB test file…")
        source = _random_file(Path(temp) / "big.bin", size)
        # Raw upload: the same chunks, their hashes worked out beforehand, sent back to back
        # (read from disk as they go, never the whole file in memory).
        body = UploadCreateIn(
            parent_id=root.root_node_id,
            name=f"raw-{time.time_ns()}.bin",
            size_bytes=size,
            sha256=source.sha256,
        )
        upload = api.data(
            "POST",
            "/api/v1/uploads",
            UploadOut,
            json=body.model_dump(mode="json", exclude_none=True),
        )
        digests: list[str] = []
        with source.path.open("rb") as handle:
            while block := handle.read(upload.chunk_size):
                digests.append(hashlib.sha256(block).hexdigest())
        started = time.perf_counter()
        with source.path.open("rb") as handle:
            for index, digest in enumerate(digests):
                api.no_content(
                    "PUT",
                    f"/api/v1/uploads/{upload.id}/chunks/{index}",
                    content=handle.read(upload.chunk_size),
                    headers={CHUNK_SHA256_HEADER: digest},
                )
        raw_up = size / (time.perf_counter() - started)
        api.no_content("DELETE", f"/api/v1/uploads/{upload.id}")

        # The client's upload: read, hash per chunk, send, complete (server verifies).
        started = time.perf_counter()
        node = files.upload(
            source, NewFile(root.root_node_id, f"client-{time.time_ns()}.bin"), stop
        )
        client_up = size / (time.perf_counter() - started)

        # Raw download: stream the content and throw it away.
        started = time.perf_counter()
        with api.stream("GET", f"/api/v1/nodes/{node.id}/content") as response:
            for _ in response.iter_bytes(1024 * 1024):
                pass
        raw_down = size / (time.perf_counter() - started)

        # The client's download: temp file, hashing, fsync, verification.
        part = Path(temp) / "download.part"
        if node.content_hash is None:
            raise RuntimeError("The server returned an upload without its hash")
        started = time.perf_counter()
        files.download(node.id, node.content_hash, size, part, stop)
        client_down = size / (time.perf_counter() - started)
    files.trash(node.id, node.revision)  # the test file goes to the trash, not left in place
    api.close()

    mib = 1024 * 1024
    up, down = client_up / raw_up, client_down / raw_down
    record(
        "NFR-PERF-04 large transfer bandwidth",
        f"at least {_TARGET:.0%} of the available bandwidth",
        f"upload {up:.0%}, download {down:.0%} of the raw transfer",
        up >= _TARGET and down >= _TARGET,
        [
            f"{args.mb} MiB file against {server_label(args.server)}",
            f"upload: client {client_up / mib:.0f} MiB/s, raw {raw_up / mib:.0f} MiB/s",
            f"download: client {client_down / mib:.0f} MiB/s, raw {raw_down / mib:.0f} MiB/s",
            "one file, one chunk at a time (chunks of one file in parallel only if below "
            "target over a real network, OQ-39)",
        ],
    )


if __name__ == "__main__":
    main()
