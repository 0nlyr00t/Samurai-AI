"""Print a report's HTML to PDF in the pinned browser.

The HTML is already the report; the PDF is the same document in a shape people
can attach to a ticket. It is printed by the browser that already runs under the
sandbox, so no new engine and no system libraries are added to the harness.
"""

from __future__ import annotations

import base64
import binascii

from samurai_ai.domain.sandbox import SandboxProfile
from samurai_ai.execution.sandbox import DockerSandboxRunner, get_profile

PDF_PROFILE_ID = "sandbox.pdf@v1"
_FENCE = "#pdf-base64\n"
MAX_PDF_BYTES = 3 * 1024 * 1024


class PdfError(RuntimeError):
    pass


_PRINT_SCRIPT = """
import base64, os, sys
from camoufox.sync_api import Camoufox

html = sys.stdin.buffer.read()
work = "/tmp/report"
os.makedirs(work, exist_ok=True)
source = os.path.join(work, "report.html")
target = os.path.join(work, "report.pdf")
with open(source, "wb") as handle:
    handle.write(html)
with Camoufox(headless=True) as browser:
    page = browser.new_page()
    page.goto("file://" + source, wait_until="load")
    page.pdf(path=target, format="A4", print_background=True,
             margin={"top": "18mm", "bottom": "18mm", "left": "14mm", "right": "14mm"})
with open(target, "rb") as handle:
    data = handle.read()
sys.stdout.write("\\\\n#pdf-base64\\\\n")
sys.stdout.write(base64.b64encode(data).decode())
"""


class PdfPrinter:
    """Render HTML to PDF bytes through the sandbox; the HTML never hits disk here."""

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        self._runner = runner

    @property
    def runner(self) -> DockerSandboxRunner:
        if self._runner is None:
            from samurai_ai.config import sandbox_allowlist_path, vault_key_path
            from samurai_ai.execution.process import ProcessManager
            from samurai_ai.execution.sandbox import DockerSandboxRunner
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
            "python3",
            ["-c", _PRINT_SCRIPT],
            None,
            env={"HOME": "/tmp", "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/root/.cache"},
            stdin_data=html.encode("utf-8"),
        )
        if outcome.result.timed_out:
            raise PdfError("the browser timed out while printing")
        stdout = outcome.result.stdout
        marker = stdout.find(_FENCE)
        if marker < 0:
            detail = (outcome.result.stderr or stdout).strip().splitlines()
            raise PdfError(
                "the browser printed no PDF"
                + (f": {detail[-1]}" if detail else "")
            )
        encoded = stdout[marker + len(_FENCE):].strip()
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
