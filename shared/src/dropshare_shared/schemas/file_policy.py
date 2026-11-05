"""What the server accepts as a file (FR-SAFE-03, NFR-SCALE-01), so clients check first."""

from pydantic import BaseModel


class FilePolicyOut(BaseModel):
    # In `naming.normalize_extension` form (".exe"); compare with `naming.is_banned`.
    banned_extensions: list[str]
    max_file_size_bytes: int
