"""Print a report's HTML to PDF in a pinned headless browser.

The HTML is already the report; the PDF is the same document in a shape people can
attach to a ticket. Chromium does the printing inside the sandbox profile
`sandbox.pdf@v1`: no new engine in the harness, no system libraries on the host,
and nothing leaves the container (the document is local, the network is off).

Firefox/camoufox was tried first and rejected: Playwright cannot print from
Firefox (`page.pdf` is Chromium-only) and the browser's own `--print-to-pdf` flag
hangs or exits without a file in that build.
"""

from __future__ import annotations

import base64
import binascii

from samurai_ai.domain.sandbox import SandboxProfile
from samurai_ai.execution.sandbox import DockerSandboxRunner, get_profile

PDF_PROFILE_ID = "sandbox.pdf@v1"
MAX_PDF_BYTES = 3 * 1024 * 1024


class PdfError(RuntimeError):
    pass


# reads HTML on stdin, prints the PDF as base64 on stdout; the container throws
# its own filesystem away, so the document has to come back through stdout
_PRINT_SCRIPT = """
mkdir -p /tmp/chrome /tmp/home /tmp/fc || exit 1
# the image has no Latin default: fontconfig would pick a CJK face and the
# printed glyphs come out as garbage, so point sans-serif at FreeSans
cat > /tmp/fonts.conf <<'FONTCONFIG'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>/usr/share/fonts</dir>
  <cachedir>/tmp/fc</cachedir>
  <match target="pattern">
    <test name="family"><string>sans-serif</string></test>
    <edit name="family" mode="assign" binding="strong"><string>FreeSans</string></edit>
  </match>
</fontconfig>
FONTCONFIG
base64 -d > /tmp/report.html || exit 1
export HOME=/tmp/home FONTCONFIG_FILE=/tmp/fonts.conf
chromium-browser --headless=new --no-sandbox --disable-gpu \\
  --disable-dev-shm-usage --user-data-dir=/tmp/chrome \\
  --no-pdf-header-footer --print-to-pdf=/tmp/report.pdf \\
  file:///tmp/report.html >/dev/null 2>&1 || exit 1
base64 -w 0 /tmp/report.pdf
"""


class PdfPrinter:
    """Render HTML to PDF bytes through the sandbox; the HTML never hits host disk."""

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        self._runner = runner

    @property
    def runner(self) -> DockerSandboxRunner:
        if self._runner is None:
            from samurai_ai.config import sandbox_allowlist_path, vault_key_path
            from samurai_ai.execution.process import ProcessManager
            from samurai_ai.services.vault import load_master_key

            self._runner = DockerSandboxRunner(
                ProcessManager(allowed_executables=frozenset({"docker"})),
                sandbox_allowlist_path(),
                lambda: load_master_key(vault_key_path()),
            )
        return self._runner

    def print(self, html: str, run_id: str) -> bytes:
        profile = get_profile(PDF_PROFILE_ID)
        outcome = self.runner.run(
            profile,
            run_id,
            "sh",
            ["-c", _PRINT_SCRIPT],
            None,
            env={"HOME": "/tmp/home", "TMPDIR": "/tmp"},
            # the document rides on stdin as base64: the script decodes it inside
            stdin_data=base64.b64encode(html.encode("utf-8")),
        )
        if outcome.result.timed_out:
            raise PdfError("the browser timed out while printing")
        encoded = "".join(outcome.result.stdout.split())
        if not encoded:
            detail = (outcome.result.stderr or "").strip().splitlines()
            raise PdfError(
                "the browser printed no PDF"
                + (f": {detail[-1]}" if detail else "")
            )
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PdfError(f"the PDF payload is not base64: {exc}") from exc
        if not data.startswith(b"%PDF"):
            raise PdfError("the payload is not a PDF")
        if len(data) > MAX_PDF_BYTES:
            raise PdfError(f"the PDF is {len(data)} bytes, over the cap")
        return data


def profile() -> SandboxProfile:
    return get_profile(PDF_PROFILE_ID)


__all__ = ["MAX_PDF_BYTES", "PDF_PROFILE_ID", "PdfError", "PdfPrinter", "profile"]
