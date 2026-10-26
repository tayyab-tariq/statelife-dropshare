import pytest

from dropshare_shared.version import parse_version


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.1.0", (0, 1, 0)),
        ("1.10.2", (1, 10, 2)),
        (" 2.0.0 ", (2, 0, 0)),
        ("99999.0.0", (99999, 0, 0)),
    ],
)
def test_valid_versions_parse_numerically(text: str, expected: tuple[int, int, int]) -> None:
    assert parse_version(text) == expected


def test_comparison_is_numeric_not_textual() -> None:
    assert parse_version("0.10.0") > parse_version("0.9.0")


@pytest.mark.parametrize(
    "text", ["", "1.2", "1.2.3.4", "1.2.3-beta.1", "v1.2.3", "01.2.3", "123456.0.0", "1.-2.3"]
)
def test_anything_else_is_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="Not a version"):
        parse_version(text)
