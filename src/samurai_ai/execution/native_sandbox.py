"""OS-level confinement for host ``shell.exec``, via bubblewrap (``bwrap``).

The structured adapters (recon.*, scan.*, fuzz.*) run inside the Docker sandbox;
``shell.exec`` runs natively on the operator host, which is where it needs the
pipelines and substitutions a container makes awkward — but natively it also had
the operator's whole filesystem. This wraps the host command in an unprivileged
``bwrap`` namespace so the kernel, not a static command parser, enforces the
boundary: a write lands only under a trusted root, and ``eval`` or a shell
variable cannot reach around it.

Two profiles:

- ``write`` (default): the whole host is bind-mounted read-only, so every tool
  keeps working and DNS/resolv.conf are untouched, but only the trusted roots
  and a private ``/tmp`` are writable. Writes outside fail with a read-only
  filesystem error. Reads stay open.
- ``strict``: only the system directories are mounted (read-only) and the
  trusted roots writable, so other users' homes, ``~/.ssh`` and ``~/.bash_history``
  outside the workspace are not even readable. Stronger, but a tool that reads an
  unusual path may miss it.

``bwrap`` is Linux-only and unprivileged; where it is absent the caller falls
back to a native run, so the sandbox is a hardening layer, never a hard
dependency.
"""

from __future__ import annotations

import shutil
import sys

#: Read-only system directories the ``strict`` profile exposes. ``*-try`` means a
#: missing one is skipped, so the same list is portable across distributions.
_STRICT_SYSTEM_DIRS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc",
    "/opt",
    "/var",
    "/run",
)


def bwrap_available() -> bool:
    """True when host-command confinement can be applied on this machine."""
    return sys.platform.startswith("linux") and shutil.which("bwrap") is not None


def build_bwrap_argv(
    inner_argv: list[str] | tuple[str, ...],
    writable_roots: tuple[str, ...],
    *,
    strict: bool = False,
    chdir: str | None = None,
) -> list[str]:
    """Wrap a host command so it can only write under ``writable_roots``.

    The returned argv runs ``inner_argv`` inside a bubblewrap namespace. Network
    is left shared (an operation reaches its targets), the process tree, IPC and
    hostname are isolated, ``/tmp`` is a private tmpfs, and each writable root is
    bind-mounted read-write. The ``write`` profile adds a read-only bind of the
    whole host; ``strict`` instead exposes only the system directories, so paths
    outside them and the roots are invisible.
    """
    roots = tuple(dict.fromkeys(root for root in writable_roots if root))
    argv: list[str] = [
        "bwrap",
        "--die-with-parent",
        "--unshare-user-try",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
    ]
    if strict:
        for directory in _STRICT_SYSTEM_DIRS:
            argv += ["--ro-bind-try", directory, directory]
    else:
        argv += ["--ro-bind", "/", "/"]
    # a private /tmp (and /var/tmp) so scratch files never touch the host's
    argv += ["--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]
    for root in roots:
        argv += ["--bind", root, root]
    if chdir:
        argv += ["--chdir", chdir]
    elif roots:
        argv += ["--chdir", roots[0]]
    argv.append("--")
    argv += list(inner_argv)
    return argv
