"""The tool-output distiller: structured facts and smart truncation."""

from __future__ import annotations

from samurai_ai.services.tool_digest import detect_tool, summarize_for_model


def test_detect_skips_wrappers_and_env() -> None:
    assert detect_tool("sudo nmap -sT 10.0.0.1") == "nmap"
    assert detect_tool("FOO=bar ffuf -u http://x/FUZZ -w list") == "ffuf"
    assert detect_tool("/usr/bin/nuclei -target x") == "nuclei"
    assert detect_tool("cat /etc/passwd") is None
    assert detect_tool("nmap -sT x | grep open") == "nmap"


def test_nmap_text_summary_lists_open_ports() -> None:
    stdout = (
        "Starting Nmap 7.95\n"
        "Nmap scan report for 10.10.10.5\n"
        "Host is up (0.012s latency).\n"
        "PORT   STATE SERVICE\n"
        "22/tcp open  ssh     OpenSSH 8.9p1\n"
        "80/tcp open  http    nginx 1.18\n"
        "443/tcp closed https\n"
        "Nmap done: 1 IP address (1 host up)\n"
    )
    out = summarize_for_model("nmap -sT -sV 10.10.10.5", stdout, "", 0, run_id="r1")
    assert "22/tcp open ssh" in out
    assert "80/tcp open http" in out
    assert "443" not in out  # closed port dropped
    assert "run=r1" in out
    assert "nmap 1.18" not in out  # never leak the whole raw dump wholesale
    assert len(out) < len(stdout) + 120


def test_nmap_no_open_ports_still_speaks() -> None:
    stdout = (
        "Nmap scan report for example.test\n"
        "All 1000 scanned ports are in ignored states.\n"
        "Nmap done: 1 IP address\n"
    )
    out = summarize_for_model("nmap example.test", stdout, "", 0, run_id="r2")
    assert "no open ports" in out
    assert "example.test" in out


def test_ffuf_text_and_json() -> None:
    text = (
        "admin                   [Status: 200, Size: 100, Words: 3, Lines: 1]\n"
        "secret                  [Status: 403, Size: 20, Words: 1, Lines: 1]\n"
    )
    out = summarize_for_model("ffuf -u http://x/FUZZ -w w", text, "", 0, run_id="f1")
    assert "admin  [200]  len=100" in out
    assert "secret  [403]  len=20" in out

    jsonl = (
        '{"input":{"FUZZ":"admin"},"status":200,"length":100}\n'
        '{"input":{"FUZZ":"api"},"status":301,"length":0}\n'
    )
    out_json = summarize_for_model("ffuf -of json -u http://x/FUZZ", jsonl, "", 0, run_id="f2")
    assert "admin  [200]  len=100" in out_json
    assert "api  [301]  len=0" in out_json


def test_nuclei_text_and_jsonl() -> None:
    text = (
        "[CVE-2021-41773] [http] [critical] https://target/cgi-bin\n"
        "[tech-detect] [http] [info] https://target\n"
    )
    out = summarize_for_model("nuclei -target https://target", text, "", 0, run_id="n1")
    assert "[critical] CVE-2021-41773 @ https://target/cgi-bin" in out
    assert "[info] tech-detect" in out

    jsonl = '{"template-id":"xss","info":{"severity":"high"},"matched-at":"https://t/q"}\n'
    out_json = summarize_for_model("nuclei -jsonl -target https://t", jsonl, "", 0, run_id="n2")
    assert "[high] xss @ https://t/q" in out_json


def test_http_headers_summary() -> None:
    stdout = (
        "HTTP/1.1 302 Found\r\n"
        "Server: Apache/2.4\r\n"
        "Location: /dashboard\r\n"
        "Set-Cookie: session=abc; HttpOnly\r\n"
        "Date: Mon, 01 Jan 2024 00:00:00 GMT\r\n"
    )
    out = summarize_for_model("curl -sI http://x", stdout, "", 0, run_id="c1")
    assert "HTTP 302 Found" in out
    assert "Server: Apache/2.4" in out
    assert "Location: /dashboard" in out
    assert "Date:" not in out  # noise header dropped


def test_unknown_tool_truncates_head_and_tail() -> None:
    stdout = "\n".join(f"line {i}" for i in range(200))
    out = summarize_for_model("weirdtool --run", stdout, "", 0, run_id="u1")
    assert "line 0" in out
    assert "line 199" in out
    assert "lines hidden" in out
    assert "line 100" not in out
    assert len(out) < len(stdout)


def test_unknown_collapses_repeats_and_progress() -> None:
    stdout = "\n".join(["same"] * 50 + ["unique"])
    out = summarize_for_model("spammer", stdout, "", 0, run_id="u2")
    assert "(×50)" in out
    assert "collapsed" in out


def test_failed_known_tool_falls_back_to_raw() -> None:
    # a nonzero exit means the parser output is unreliable; show cleaned raw.
    out = summarize_for_model("nmap bad", "", "nmap: command not found", 127, run_id="e1")
    assert "command not found" in out
    assert "run=e1" in out


def test_pointer_present_even_without_run_id() -> None:
    out = summarize_for_model("echo hi", "hi", "", 0)
    assert "full output in UI" in out
