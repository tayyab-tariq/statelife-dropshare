"""How sizes and similar values read in the UI (NFR-UX-03)."""

_UNITS = ("KB", "MB", "GB", "TB")


def size_text(size: int) -> str:
    """1,000-based like Windows Explorer's wording, with one decimal from KB up."""
    if size < 1000:
        return "1 byte" if size == 1 else f"{size} bytes"
    value = float(size)
    for unit in _UNITS:
        value /= 1000
        if value < 1000 or unit == _UNITS[-1]:
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")
