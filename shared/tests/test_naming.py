import unicodedata

import pytest

from dropshare_shared.naming import (
    MAX_NAME_LENGTH,
    NAME_PROBLEM_MESSAGES,
    InvalidNameError,
    NameProblem,
    normalize_name,
    validate_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "report.docx",
        "Résumé 2026.pdf",
        ".gitignore",
        "CONSOLE.txt",
        "con-notes.md",
        "COM10",
        "LPT0.log",
        "a" * MAX_NAME_LENGTH,
        "日本語のファイル",
        "file with  spaces inside",
    ],
)
def test_valid_names_are_accepted(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("", NameProblem.EMPTY),
        ("a" * (MAX_NAME_LENGTH + 1), NameProblem.TOO_LONG),
        *[(f"bad{char}name", NameProblem.FORBIDDEN_CHARACTER) for char in '<>:"/\\|?*'],
        ("tab\tname", NameProblem.CONTROL_CHARACTER),
        ("nul\x00byte", NameProblem.CONTROL_CHARACTER),
        ("del\x7fchar", NameProblem.CONTROL_CHARACTER),
        ("trailing.", NameProblem.TRAILING_DOT_OR_SPACE),
        ("trailing ", NameProblem.TRAILING_DOT_OR_SPACE),
        (".", NameProblem.TRAILING_DOT_OR_SPACE),
        ("..", NameProblem.TRAILING_DOT_OR_SPACE),
        ("CON", NameProblem.RESERVED),
        ("con", NameProblem.RESERVED),
        ("Prn.txt", NameProblem.RESERVED),
        ("aux.tar.gz", NameProblem.RESERVED),
        ("NUL .txt", NameProblem.RESERVED),
        ("COM1", NameProblem.RESERVED),
        ("com9.log", NameProblem.RESERVED),
        ("LPT5", NameProblem.RESERVED),
        ("COM¹", NameProblem.RESERVED),
    ],
)
def test_invalid_names_are_rejected_with_reason(name: str, problem: NameProblem) -> None:
    with pytest.raises(InvalidNameError) as caught:
        validate_name(name)
    assert caught.value.problem is problem


def test_length_counts_utf16_units_like_ntfs() -> None:
    # Emoji are outside the BMP: two UTF-16 units each.
    assert validate_name("😀" * 127) == "😀" * 127
    with pytest.raises(InvalidNameError) as caught:
        validate_name("😀" * 128)
    assert caught.value.problem is NameProblem.TOO_LONG


@pytest.mark.parametrize("name", ["\ud800", "bad\udfffname.txt"])
def test_lone_surrogates_are_rejected(name: str) -> None:
    with pytest.raises(InvalidNameError) as caught:
        validate_name(name)
    assert caught.value.problem is NameProblem.INVALID_UNICODE


def test_every_problem_has_a_stable_code_and_a_message() -> None:
    for problem in NameProblem:
        assert problem.value == problem.name
        assert NAME_PROBLEM_MESSAGES[problem]
    assert str(InvalidNameError(NameProblem.EMPTY)) == NAME_PROBLEM_MESSAGES[NameProblem.EMPTY]


def test_validate_returns_nfc_form() -> None:
    decomposed = unicodedata.normalize("NFD", "Résumé.pdf")
    assert decomposed != "Résumé.pdf"
    assert validate_name(decomposed) == "Résumé.pdf"


def test_length_is_measured_after_nfc() -> None:
    # 255 "é" in NFD is 510 code points but only 255 characters in NFC.
    name = unicodedata.normalize("NFD", "é" * MAX_NAME_LENGTH)
    assert validate_name(name) == "é" * MAX_NAME_LENGTH


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Report.docx", "report.DOCX"),
        (unicodedata.normalize("NFD", "Résumé"), "RÉSUMÉ"),
        ("Straße", "STRASSE"),
    ],
)
def test_normalized_names_collide_case_insensitively(first: str, second: str) -> None:
    assert normalize_name(first) == normalize_name(second)


def test_different_names_do_not_collide() -> None:
    assert normalize_name("report-1.docx") != normalize_name("report-2.docx")
