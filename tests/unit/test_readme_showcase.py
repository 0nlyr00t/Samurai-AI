"""The README is the shop window: keep its promises pointing at real files."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def readme(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_the_logo_stays_on_top_of_both_readmes() -> None:
    for name in ("README.md", "README.en.md"):
        text = readme(name)
        assert text.lstrip().startswith("<p align=\"center\">")
        assert "assets/samurai-logo.jpg" in text.split("\n## ")[0]


def test_both_readmes_show_a_screenshot_that_exists() -> None:
    for name, shot in (
        ("README.md", "assets/samurai-tui.ru.png"),
        ("README.en.md", "assets/samurai-tui.en.png"),
    ):
        text = readme(name)
        assert shot in text
        assert (ROOT / shot).exists()
        # the shot is generated, so both a source SVG and the generator exist
        assert (ROOT / shot.replace(".png", ".svg")).exists()
        assert (ROOT / "ops" / "screenshot.py").exists()


def test_the_readmes_describe_install_and_uninstall() -> None:
    for name in ("README.md", "README.en.md"):
        text = readme(name)
        assert "install.sh" in text and "uninstall.sh" in text


def test_the_capability_detail_moved_but_is_linked() -> None:
    for name in ("README.md", "README.en.md"):
        assert "md/capabilities.md" in readme(name)
    detail = (ROOT / "md" / "capabilities.md").read_text(encoding="utf-8")
    assert "Поставка" in detail and "Модели" in detail


def test_the_language_switch_is_mutual() -> None:
    assert "README.en.md" in readme("README.md")
    assert "README.md" in readme("README.en.md")
