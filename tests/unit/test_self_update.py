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
