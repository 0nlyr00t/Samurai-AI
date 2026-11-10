# Sandbox wordlists

Files here are fed to sandboxed fuzzers over stdin (the sandbox does not mount
host paths), so every file must fit the 64 KiB stdin payload cap and must carry
a pinned sha256 that a test checks.

## common.txt

- Source: [SecLists](https://github.com/danielmiessler/SecLists)
  `Discovery/Web-Content/common.txt`, commit
  `0c618cfc28d1e68811b95c61cc9ab05bc10b0a02` (2026-11-02).
- sha256: `47fb86ca6fb3f97e5491161581900d6d99851fb16764eacddf16aa85617c956a`
- 4751 lines, 38 KiB.

The full `raft-small-directories.txt` (163 KiB) does not fit the cap yet; it
waits for an async stdin channel or an own image.
