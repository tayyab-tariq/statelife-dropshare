"""Sizes as the UI shows them."""

import pytest

from dropshare_client.ui.format import size_text


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 bytes"),
        (1, "1 byte"),
        (999, "999 bytes"),
        (1000, "1.0 KB"),
        (2_100_000, "2.1 MB"),
        (5_000_000_000, "5.0 GB"),
        (3 * 10**15, "3000.0 TB"),
    ],
)
def test_sizes(size: int, text: str) -> None:
    assert size_text(size) == text
