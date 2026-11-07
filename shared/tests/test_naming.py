import unicodedata

import pytest

from dropshare_shared.naming import (
    MAX_NAME_LENGTH,
    NAME_PROBLEM_MESSAGES,
    InvalidNameError,
    NameProblem,
    file_extension,
    is_banned,
    normalize_extension,
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


# FR-SAFE-02: the last extension of the normalized name, compared case-insensitively.
@pytest.mark.parametrize(
    ("name", "extension"),
    [
        ("setup.exe", ".exe"),
        ("SETUP.EXE", ".exe"),
        ("Setup.ExE", ".exe"),
        ("invoice.pdf.EXE", ".exe"),
        ("archive.tar.gz", ".gz"),
        ("report.exe.pdf", ".pdf"),
        ("exe.txt", ".txt"),
        (".exe", ".exe"),
        (".gitignore", ".gitignore"),
        ("README", None),
        ("exe", None),
        ("trailing.", None),
        ("Résumé.PDF", ".pdf"),
        ("caf\u0065\u0301.\u00c9XE", ".\u00e9xe"),  # NFD stem, NFC extension
        ("x.\u0045\u0301XE", ".\u00e9xe"),  # NFD extension
        ("photo.\u0130", ".i\u0307"),  # casefold of dotted capital I
        ("a" * 251 + ".exe", ".exe"),
        ("名前.EXE", ".exe"),
        ("📄.exe", ".exe"),
        ("name .exe", ".exe"),
        ("a. exe", ". exe"),
    ],
)
def test_file_extension_is_the_last_extension_casefolded(name: str, extension: str | None) -> None:
    assert file_extension(name) == extension


_BANNED = frozenset({".exe", ".ps1", ".js"})


@pytest.mark.parametrize(
    ("name", "banned"),
    [
        ("setup.exe", True),
        ("SETUP.EXE", True),
        ("invoice.pdf.Exe", True),
        ("script.PS1", True),
        (".exe", True),
        ("app.js", True),
        ("report.exe.pdf", False),
        ("exe.txt", False),
        ("exe", False),
        ("notes.json", False),  # ".js" is not a prefix match
        ("setup.exe1", False),
        ("setup.xe", False),
        ("README", False),
    ],
)
def test_is_banned_matches_only_the_last_extension(name: str, banned: bool) -> None:
    assert is_banned(name, _BANNED) is banned


def test_is_banned_with_an_empty_list_allows_everything() -> None:
    assert not is_banned("setup.exe", frozenset())


@pytest.mark.parametrize(
    ("entry", "normalized"),
    [(".exe", ".exe"), (".EXE", ".exe"), (".Ps1", ".ps1"), (".\u0045\u0301", ".\u00e9")],
)
def test_normalize_extension_gives_the_comparison_form(entry: str, normalized: str) -> None:
    assert normalize_extension(entry) == normalized
    assert is_banned("file" + entry, {normalized})


@pytest.mark.parametrize(
    "entry",
    ["", ".", "exe", "*.exe", ".tar.gz", "..exe", ".ex/e", ".ex:e", ".e\x00", ".exe "],
)
def test_normalize_extension_refuses_entries_that_could_never_match(entry: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        normalize_extension(entry)
