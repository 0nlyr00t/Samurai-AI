"""No slogan strings: the arc text on the logo is part of the picture, not a product line."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLOGANS = ("red team ai harness", "one shot", "many targets")


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8").lower()


def test_the_readmes_do_not_print_the_slogan() -> None:
    for name in ("README.md", "README.en.md"):
        text = read(name)
        for slogan in SLOGANS:
            assert slogan not in text, f"{name} still says {slogan!r}"


def test_the_installer_and_uninstaller_do_not_print_it() -> None:
    for name in ("install.sh", "uninstall.sh"):
        text = read(name)
        for slogan in SLOGANS:
            assert slogan not in text, f"{name} still says {slogan!r}"


def test_the_requirements_say_the_slogan_is_gone() -> None:
    text = read("md/requirements.md")
    assert "red team ai harness" not in text
    assert "слогана как отдельной текстовой сущности нет" in text


def test_the_logo_alt_describes_the_picture_without_a_slogan() -> None:
    for name in ("README.md", "README.en.md"):
        first_block = (ROOT / name).read_text(encoding="utf-8").split("\n## ")[0]
        assert "samurai-logo.jpg" in first_block
        assert "вордмарк" in first_block.lower() or "wordmark" in first_block.lower()
