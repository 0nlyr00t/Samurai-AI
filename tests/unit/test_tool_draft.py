"""Drafting a tool from documentation: redaction, confirmation, sidecars."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from samurai_ai.services import drafts
from samurai_ai.services.drafts import (
    MAX_DOC_BYTES,
    DocReader,
    DraftError,
    DraftService,
    assess,
    read_meta,
    redact,
    sidecar_path,
)

DIGEST = "0123456789abcdef" * 4

DOC = f"""\
# samuraidemo

Install on Debian: `apt-get install samuraidemo`; on macOS: `brew install samuraidemo`.

Image: ghcr.io/acme/samuraidemo@sha256:{DIGEST}

Check with `samuraidemo --version`.

API key: api_key=supersecretvalue
"""


def candidate(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "scan.samuraidemo",
        "title": "Samurai demo scan",
        "image": f"ghcr.io/acme/samuraidemo@sha256:{DIGEST}",
        "executable": "samuraidemo",
        "risk_class": "r1",
        "network": "scoped",
        "read_only": True,
        "proxy_flag": "-proxy",
        "execution": {"native": True, "project_local": False, "container": True},
        "provision": {
            "packages": {
                "linux": {"manager": "apt", "package": "samuraidemo"},
                "macos": {"manager": "brew", "package": "samuraidemo"},
            },
            "healthcheck": ["--version"],
        },
        "params": {"target": {"type": "string", "required": True}},
        "targets": {"param": "target", "kind": "host", "max": 1},
        "argv": ["-t", "{target}"],
        "parser": "text-lines",
    }
    payload.update(overrides)
    return payload


def write_doc(tmp_path: Path, text: str = DOC) -> Path:
    path = tmp_path / "doc.md"
    path.write_text(text, encoding="utf-8")
    return path


def service(answer: dict[str, object] | None = None, **kwargs: object) -> DraftService:
    body = json.dumps(candidate() if answer is None else answer)
    return DraftService(extractor=lambda _doc: body, **kwargs)  # type: ignore[arg-type]


# --- redaction -----------------------------------------------------------


def test_secrets_are_redacted_and_counted() -> None:
    text = (
        "Authorization: Bearer abcdefghijkl\n"
        "api_key=secretvalue\n"
        'curl -H "X-Api-Key: 12345678" https://user:pw@example.com/x\n'
        "token: sk-abcdefghijklmnopqrstu\n"
        "AKIAIOSFODNN7EXAMPLE\n"
    )
    redacted, count = redact(text)
    assert "abcdefghijkl" not in redacted
    assert "secretvalue" not in redacted
    assert "user:pw@" not in redacted
    assert "sk-abcdefghijklmnopqrstu" not in redacted
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED]" in redacted
    assert count >= 5


def test_private_key_blocks_are_redacted() -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nAAA\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    redacted, count = redact(text)
    assert "MIIEow" not in redacted
    assert count == 1


def test_a_digest_survives_redaction() -> None:
    # a sha256 digest must never be mistaken for a secret: it is the pin
    redacted, count = redact(f"image sha256:{DIGEST}\n")
    assert DIGEST in redacted
    assert count == 0


# --- reading -------------------------------------------------------------


def test_offline_refuses_a_url_before_fetching() -> None:
    calls: list[str] = []

    def fetcher(url: str, _timeout: float, _limit: int) -> tuple[bytes, bool]:
        calls.append(url)
        return b"body", False

    reader = DocReader(fetcher=fetcher)
    with pytest.raises(DraftError, match="offline"):
        reader.read("https://example.com/doc", offline=True)
    assert calls == []


def test_a_local_file_is_read_while_offline(tmp_path: Path) -> None:
    path = write_doc(tmp_path)
    captured = DocReader().read(str(path), offline=True)
    assert captured.kind == "file"
    assert "samuraidemo" in captured.text


def test_a_non_http_scheme_is_refused() -> None:
    with pytest.raises(DraftError, match="scheme"):
        DocReader().read("ftp://example.com/doc", offline=False)


def test_a_url_without_a_host_is_refused() -> None:
    with pytest.raises(DraftError, match="no host"):
        DocReader().read("http://", offline=False)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DraftError, match="no such documentation"):
        DocReader().read(str(tmp_path / "nope.md"), offline=False)


def test_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DraftError, match="directory"):
        DocReader().read(str(tmp_path), offline=False)


def test_documentation_is_cut_at_the_byte_ceiling(tmp_path: Path) -> None:
    path = write_doc(tmp_path, "a" * (MAX_DOC_BYTES + 500))
    captured = DocReader().read(str(path), offline=False)
    assert captured.truncated is True
    assert len(captured.text) == MAX_DOC_BYTES


class _FakeBroker:
    def __init__(self, body: bytes, status: int = 200, truncated: bool = False) -> None:
        self.body = body
        self.status = status
        self.truncated = truncated
        self.urls: list[str] = []

    def __call__(self, url: str, _timeout: float, _limit: int) -> tuple[bytes, bool]:
        self.urls.append(url)
        return self.body, self.truncated


def test_a_url_is_read_through_the_fetcher() -> None:
    fetch = _FakeBroker(b"hello docs", truncated=True)
    captured = DocReader(fetcher=fetch).read("http://127.0.0.1:9/doc", offline=False)
    assert captured.kind == "url"
    assert captured.text == "hello docs"
    assert captured.truncated is True
    assert fetch.urls == ["http://127.0.0.1:9/doc"]


def test_an_http_error_status_is_a_refusal() -> None:
    def fetch(_url: str, _timeout: float, _limit: int) -> tuple[bytes, bool]:
        raise DraftError("HTTP 404 for url")

    with pytest.raises(DraftError, match="404"):
        DocReader(fetcher=fetch).read("http://127.0.0.1:9/doc", offline=False)


def test_an_empty_body_is_a_refusal() -> None:
    with pytest.raises(DraftError, match="empty body"):
        DocReader(fetcher=_FakeBroker(b"  ")).read("http://127.0.0.1:9/doc", offline=False)


# --- confirmation --------------------------------------------------------


def test_a_confirmed_candidate_has_no_problem() -> None:
    assert assess(DOC, candidate(), templates=None) == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"title": None}, "missing required field: title"),
        ({"image": "ghcr.io/acme/samuraidemo:latest"}, "not pinned by digest"),
        (
            {"image": f"ghcr.io/acme/samuraidemo@sha256:{'f' * 64}"},
            "digest",
        ),
        ({"executable": "sdemo"}, "executable 'sdemo' is not in the documentation"),
        ({"executable": "/usr/bin/samuraidemo"}, "bare command name"),
        (
            {
                "provision": {
                    "packages": {"linux": {"manager": "apt", "package": "othertool"}},
                    "healthcheck": ["--version"],
                }
            },
            "package 'othertool' is not in the documentation",
        ),
        (
            {
                "provision": {
                    "packages": {"linux": {"manager": "apt", "package": "samuraidemo"}},
                    "healthcheck": [],
                }
            },
            "healthcheck is empty",
        ),
        (
            {
                "provision": {
                    "packages": {"linux": {"manager": "apt", "package": "samuraidemo"}}
                }
            },
            "missing required field: provision.healthcheck",
        ),
        (
            {
                "provision": {
                    "packages": {
                        "linux": {
                            "manager": "apt",
                            "package": "samuraidemo",
                            "reproducible": True,
                        }
                    },
                    "healthcheck": ["--version"],
                }
            },
            "reproducible must stay false",
        ),
        ({"execution": {"native": False, "container": False}}, "declares no path"),
        ({"argv": []}, "argv is empty"),
    ],
)
def test_an_unconfirmed_candidate_is_refused(
    overrides: dict[str, object], message: str
) -> None:
    problems = assess(DOC, candidate(**overrides), templates=None)
    assert any(message in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        # the fields are all confirmed, the shape still does not validate
        ({"argv": ["-t"]}, "used nowhere"),
        ({"params": {}}, "unknown parameters"),
        ({"parser": "yaml"}, "parser"),
    ],
)
def test_a_schema_invalid_draft_stays_incomplete(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    """assess passes it, the strict schema does not: the draft is not ready."""
    output = tmp_path / "scan.samuraidemo.json"
    draft = service(candidate(**overrides)).draft(str(write_doc(tmp_path)), output)
    assert draft.ready is False
    assert any(message in item for item in draft.missing), draft.missing


def test_a_project_local_template_must_exist(tmp_path: Path) -> None:
    payload = candidate()
    payload["provision"] = {
        "packages": {
            "linux": {"manager": "project-local", "package": "samuraidemo.py"}
        },
        "healthcheck": ["--version"],
    }
    assert any("template" in problem for problem in assess(DOC, payload, templates=tmp_path))
    (tmp_path / "samuraidemo.py").write_text("print('x')", encoding="utf-8")
    assert assess(DOC, payload, templates=tmp_path) == []


def test_healthcheck_flags_must_be_documented() -> None:
    payload = candidate()
    payload["provision"] = {
        "packages": {"linux": {"manager": "apt", "package": "samuraidemo"}},
        "healthcheck": ["--invented"],
    }
    problems = assess(DOC, payload, templates=None)
    assert any("'--invented' is not in the documentation" in item for item in problems)


def test_a_generic_flag_never_confirms_anything() -> None:
    payload = candidate()
    payload["provision"] = {
        "packages": {"linux": {"manager": "apt", "package": "samuraidemo"}},
        "healthcheck": ["--version"],
    }
    # `--version` is skipped, and the executable is the first token: clean
    assert assess(DOC, payload, templates=None) == []


def test_healthcheck_must_not_repeat_the_executable() -> None:
    payload = candidate()
    payload["provision"] = {
        "packages": {"linux": {"manager": "apt", "package": "samuraidemo"}},
        "healthcheck": ["--version", "samuraidemo"],
    }
    problems = assess(DOC, payload, templates=None)
    assert any("without the executable" in item for item in problems)


# --- drafting ------------------------------------------------------------


def _ready(tmp_path: Path, doc: Path | None = None, **kwargs: object) -> drafts.Draft:
    output = tmp_path / "out" / "scan.samuraidemo.json"
    return service(**kwargs).draft(str(doc or write_doc(tmp_path)), output)  # type: ignore[arg-type]


def test_a_ready_draft_writes_the_file_and_a_sidecar(tmp_path: Path) -> None:
    draft = _ready(tmp_path)

    assert draft.ready is True
    assert draft.missing == ()
    payload = json.loads(draft.output.read_text())
    assert payload["id"] == "scan.samuraidemo"
    # a draft is never installed, so it never claims trust
    assert payload["trusted"] is False

    meta = read_meta(draft.output)
    assert meta is not None
    assert meta.status == "ready"
    assert meta.source_kind == "file"
    assert meta.source_ref == str(tmp_path / "doc.md")
    assert meta.candidate_sha256 == drafts.hashlib.sha256(
        draft.output.read_bytes()
    ).hexdigest()
    assert draft.sidecar == sidecar_path(draft.output)


def test_an_incomplete_draft_is_written_anyway(tmp_path: Path) -> None:
    output = tmp_path / "scan.samuraidemo.json"
    draft = service({"id": "scan.samuraidemo"}).draft(str(write_doc(tmp_path)), output)

    assert draft.ready is False
    assert draft.status == "incomplete"
    assert any("missing required field: image" in item for item in draft.missing)
    assert output.is_file()
    meta = read_meta(output)
    assert meta is not None and meta.ready is False


def test_secrets_do_not_reach_the_draft_file(tmp_path: Path) -> None:
    payload = candidate(description="key is api_key=supersecretvalue")
    output = tmp_path / "scan.samuraidemo.json"
    draft = service(payload).draft(str(write_doc(tmp_path)), output)

    text = output.read_text()
    assert "supersecretvalue" not in text
    assert "[REDACTED]" in text
    assert draft.redactions >= 1


def test_the_sidecar_records_the_document_digest(tmp_path: Path) -> None:
    draft = _ready(tmp_path)
    meta = read_meta(draft.output)
    assert meta is not None
    assert meta.document_sha256 == drafts.hashlib.sha256(
        drafts.redact(DOC)[0].encode("utf-8")
    ).hexdigest()


def test_a_non_json_model_answer_is_a_refusal(tmp_path: Path) -> None:
    output = tmp_path / "scan.samuraidemo.json"
    service_with_prose = DraftService(extractor=lambda _doc: "sure! here you go")
    with pytest.raises(DraftError, match="not one JSON object"):
        service_with_prose.draft(str(write_doc(tmp_path)), output)
    assert not output.exists()


def test_no_extractor_is_a_refusal(tmp_path: Path) -> None:
    with pytest.raises(DraftError, match="extractor"):
        DraftService().draft(str(write_doc(tmp_path)), tmp_path / "out.json")


def test_the_output_parent_is_created(tmp_path: Path) -> None:
    draft = _ready(tmp_path)
    assert draft.output.parent.is_dir()


def test_a_plain_manifest_has_no_sidecar_meta(tmp_path: Path) -> None:
    path = tmp_path / "plain.json"
    path.write_text("{}", encoding="utf-8")
    assert read_meta(path) is None


def test_a_broken_sidecar_reads_as_no_meta(tmp_path: Path) -> None:
    path = tmp_path / "plain.json"
    path.write_text("{}", encoding="utf-8")
    sidecar_path(path).write_text("{not json", encoding="utf-8")
    assert read_meta(path) is None


def test_markdown_fences_around_the_answer_are_tolerated(tmp_path: Path) -> None:
    body = "```json\n" + json.dumps(candidate()) + "\n```"
    output = tmp_path / "scan.samuraidemo.json"
    draft = DraftService(extractor=lambda _doc: body).draft(str(write_doc(tmp_path)), output)
    assert draft.ready is True


def test_the_prompt_carries_a_real_json_example() -> None:
    """The example must be parseable-shaped, not doubled braces."""
    prompt = drafts._PROMPT
    assert '{{"' not in prompt
    assert '{"id": "scan.example"' in prompt
    assert "Never invent a digest" in prompt
    assert "verbatim" in prompt
