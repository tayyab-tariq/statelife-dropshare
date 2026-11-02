"""Values an installer build bakes in (FR-CLIENT-05, FR-UPD-03).

`scripts/build_client.ps1` writes them to `build_info.json`, bundled beside this module. From
source there is no such file and development values apply: the local server, and no expected
publisher, so updates are never installed; nor does an unsigned test build, which pins no
certificate. A frozen build without a valid file is broken and refuses to start rather than
quietly pointing at the wrong server.
"""

import sys
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dropshare_shared.signing import ExpectedSigner, normalize_thumbprint

_FILE = Path(__file__).with_name("build_info.json")


class BuildInfoError(RuntimeError):
    """An installed build has no valid build_info.json."""


class BuildInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server_url: str
    # Who must have signed every update (FR-UPD-03): the certificate's name, and the SHA-256 of
    # one of these certificates (several while a renewed certificate is rolled out).
    publisher: str = Field(max_length=255)
    cert_sha256: list[str] = []

    @field_validator("server_url")
    @classmethod
    def _https(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("an installer build needs an https:// server address")
        return value.strip().rstrip("/")

    @field_validator("cert_sha256")
    @classmethod
    def _thumbprints(cls, values: list[str]) -> list[str]:
        return [normalize_thumbprint(value) for value in values]

    def update_signer(self) -> ExpectedSigner:
        return ExpectedSigner.of(self.publisher, self.cert_sha256)


DEVELOPMENT = BuildInfo.model_construct(
    server_url="http://127.0.0.1:8000", publisher="", cert_sha256=[]
)


def load(path: Path = _FILE, *, frozen: bool | None = None) -> BuildInfo:
    if not (getattr(sys, "frozen", False) if frozen is None else frozen):
        return DEVELOPMENT
    try:
        info = BuildInfo.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise BuildInfoError(f"This build of DropShare is incomplete ({path.name}).") from exc
    if not info.publisher:
        raise BuildInfoError("This build of DropShare names no publisher for its updates.")
    return info


BUILD = load()
