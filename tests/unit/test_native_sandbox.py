"""The bwrap wrapper confines a host command to its writable roots."""

from __future__ import annotations

from samurai_ai.execution import native_sandbox
from samurai_ai.execution.native_sandbox import build_bwrap_argv, bwrap_available


def test_write_profile_binds_root_read_only_and_roots_writable() -> None:
    argv = build_bwrap_argv(["bash", "-lc", "id"], ("/home/op/work",))
    assert argv[0] == "bwrap"
    # the whole host is read-only, only the trusted root is writable
    i = argv.index("--ro-bind")
    assert argv[i + 1 : i + 3] == ["/", "/"]
    assert _pair(argv, "--bind", "/home/op/work") == [
        "--bind",
        "/home/op/work",
        "/home/op/work",
    ]
    # a private tmp, and the command runs after the `--` separator
    assert "--tmpfs" in argv
    assert argv[argv.index("--") + 1 :] == ["bash", "-lc", "id"]
    # chdir defaults to the first root
    assert _pair(argv, "--chdir") == ["--chdir", "/home/op/work"]


def test_dev_and_proc_overlay_after_the_readonly_root() -> None:
    """A whole-host ro-bind must not shadow the virtual /dev (breaks ssh)."""
    argv = build_bwrap_argv(["bash", "-lc", "id"], ("/home/op/work",))
    ro_root = argv.index("--ro-bind")
    assert argv[ro_root + 1 : ro_root + 3] == ["/", "/"]
    # /dev and /proc are overlaid AFTER the read-only root, or the host's
    # read-only /dev re-binds over them and /dev/null + the pty become unusable
    assert argv.index("--dev") > ro_root
    assert argv.index("--proc") > ro_root
    assert _pair(argv, "--dev") == ["--dev", "/dev"]


def test_strict_profile_exposes_only_system_dirs_not_the_whole_host() -> None:
    argv = build_bwrap_argv(["bash", "-lc", "id"], ("/home/op/work",), strict=True)
    # no blanket read-only root in strict mode: home outside the root is invisible
    assert "--ro-bind" not in argv or "/" not in _pair(argv, "--ro-bind")
    assert "--ro-bind-try" in argv
    assert ["--bind", "/home/op/work", "/home/op/work"] == _pair(argv, "--bind", "/home/op/work")


def test_multiple_roots_are_each_bound_and_deduped() -> None:
    argv = build_bwrap_argv(["sh", "-c", "x"], ("/a", "/b", "/a", ""))
    assert argv.count("--bind") == 2  # /a once, /b once, blank dropped
    assert "--chdir" in argv and _pair(argv, "--chdir") == ["--chdir", "/a"]


def test_bwrap_available_needs_linux_and_the_binary(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(native_sandbox.sys, "platform", "linux")
    monkeypatch.setattr(native_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    assert bwrap_available() is True
    monkeypatch.setattr(native_sandbox.shutil, "which", lambda name: None)
    assert bwrap_available() is False
    monkeypatch.setattr(native_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(native_sandbox.sys, "platform", "darwin")
    assert bwrap_available() is False


def _pair(argv: list[str], flag: str, value: str | None = None) -> list[str]:
    for index, token in enumerate(argv):
        if token == flag and (value is None or argv[index + 1] == value):
            return argv[index : index + 3 if value is not None else index + 2]
    return []
