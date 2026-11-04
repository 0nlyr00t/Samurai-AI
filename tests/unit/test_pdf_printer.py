"""The printer decodes what the browser returns and refuses everything else."""

import base64

import pytest

from samurai_ai.domain.sandbox import SandboxProfile
from samurai_ai.execution.pdf import PdfError, PdfPrinter, profile


class FakeResult:
    def __init__(self, stdout: str, stderr: str = "", timed_out: bool = False) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


class FakeOutcome:
    def __init__(self, result: FakeResult) -> None:
        self.result = result


class FakeRunner:
    def __init__(self, result: FakeResult) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def run(self, profile: SandboxProfile, run_id, executable, args, scope, **kwargs):
        self.calls.append((profile.id, executable, kwargs.get("stdin_data")))
        return FakeOutcome(self.result)


def printer(result: FakeResult) -> tuple[PdfPrinter, FakeRunner]:
    runner = FakeRunner(result)
    return PdfPrinter(runner), runner  # type: ignore[arg-type]


def test_a_pdf_comes_back_as_bytes() -> None:
    payload = b"%PDF-1.7 fake"
    instance, runner = printer(FakeResult("\n" + base64.b64encode(payload).decode() + "\n"))
    assert instance.print("<html></html>", "run_1") == payload
    profile_id, executable, stdin = runner.calls[0]
    assert profile_id == "sandbox.pdf@v1" and executable == "sh"
    # the document rides on stdin as base64, the script decodes it inside
    assert base64.b64decode(stdin) == b"<html></html>"


def test_the_profile_is_local_only_pinned_and_unprivileged() -> None:
    pdf_profile = profile()
    assert pdf_profile.network_mode == "none"
    assert "@sha256:" in pdf_profile.image
    assert pdf_profile.allowed_executables == ("sh", "chromium-browser")
    assert pdf_profile.user == "65534:65534"


def test_an_empty_answer_is_a_refusal() -> None:
    instance, _ = printer(FakeResult("", stderr="boom"))
    with pytest.raises(PdfError, match="printed no PDF: boom"):
        instance.print("<html></html>", "run_1")


def test_a_non_pdf_payload_is_a_refusal() -> None:
    instance, _ = printer(FakeResult(base64.b64encode(b"<html>").decode()))
    with pytest.raises(PdfError, match="not a PDF"):
        instance.print("<html></html>", "run_1")


def test_a_timeout_is_a_refusal() -> None:
    instance, _ = printer(FakeResult("", timed_out=True))
    with pytest.raises(PdfError, match="timed out"):
        instance.print("<html></html>", "run_1")


def test_a_wrapped_payload_is_rejoined() -> None:
    payload = b"%PDF-1.7 " + b"x" * 200
    encoded = base64.b64encode(payload).decode()
    wrapped = "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60)) + "\n"
    instance, _ = printer(FakeResult(wrapped))
    assert instance.print("<html></html>", "run_1") == payload
