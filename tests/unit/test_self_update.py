from samurai_ai.self_update import (
    is_newer,
    parse_latest_tag,
    version_key,
)

LS_REMOTE_SAMPLE = """\
abc123\trefs/tags/v0.1.0
def456\trefs/tags/v0.2.0
789aaa\trefs/tags/v0.2.0^{}
bbb222\trefs/tags/v0.10.1
ccc333\trefs/tags/not-a-version
"""


def test_version_key_orders_semver_numerically() -> None:
    assert version_key("v0.10.1") > version_key("0.9.9")
    assert version_key("1.0.0") > version_key("0.99.99")
    assert version_key("garbage") == (0, 0, 0)


def test_parse_latest_tag_picks_highest_semver() -> None:
    assert parse_latest_tag(LS_REMOTE_SAMPLE) == "0.10.1"


def test_parse_latest_tag_empty() -> None:
    assert parse_latest_tag("") is None
    assert parse_latest_tag("abc\trefs/heads/main\n") is None


def test_is_newer() -> None:
    assert is_newer("0.2.1", "0.2.0")
    assert not is_newer("0.2.0", "0.2.0")
    assert not is_newer("0.1.9", "0.2.0")


def test_a_failed_install_clears_the_uv_git_cache_and_retries(tmp_path, monkeypatch) -> None:
    """A stale git cache after a history rewrite must not strand the update."""
    from samurai_ai import self_update

    calls: list[list[str]] = []

    class FakeResult:
        def __init__(self, code: int, out: str = "") -> None:
            self.returncode = code
            self.stdout = out

    def fake_run(argv, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        if argv[1] == "cache":
            return FakeResult(0, str(tmp_path))
        # the first install fails, the retry succeeds
        return FakeResult(1 if len(calls) == 1 else 0)

    cache = tmp_path / "git-v0" / "db" / "deadbeef"
    cache.mkdir(parents=True)
    monkeypatch.setattr(self_update.shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    first = self_update.install_tag("0.5.0")
    assert first.returncode == 1
    assert self_update.clear_uv_git_cache() is True
    assert not (tmp_path / "git-v0").exists()
    second = self_update.install_tag("0.5.0")
    assert second.returncode == 0
    assert sum(1 for c in calls if c[1] == "tool") == 2


def test_clear_without_a_cache_is_a_noop(tmp_path, monkeypatch) -> None:
    from samurai_ai import self_update

    class FakeResult:
        returncode = 0
        stdout = str(tmp_path)

    monkeypatch.setattr(self_update.shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(
        self_update.subprocess, "run", lambda *_a, **_k: FakeResult()
    )
    assert self_update.clear_uv_git_cache() is False
