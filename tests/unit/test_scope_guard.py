"""The central guard: host-shaped literals out of any parameter, fail closed."""

import pytest

from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.scope_guard import ScopeGuardError, guard, infra_hosts, text_hosts

SCOPE = ScopeContract(includes=("10.10.0.0/24", "example.com", "*.example.com"))


def test_text_hosts_recognizes_urls_and_common_tlds() -> None:
    text = (
        "Исследуй https://example.co.uk и https://target.dev:8443/a, затем "
        "demo-site.app, portal.example.ai и http://10.10.11.5:8080."
    )
    assert text_hosts(text) == [
        "example.co.uk",
        "target.dev",
        "demo-site.app",
        "portal.example.ai",
        "10.10.11.5",
    ]


def test_text_hosts_ignores_files_and_invalid_addresses() -> None:
    assert text_hosts("open app.js, notes.md, module.py and 999.1.2.3") == []
    assert text_hosts("read https://example.com/README.md") == ["example.com"]


def test_a_host_inside_the_scope_passes() -> None:
    guard(SCOPE, {"target": "10.10.0.5", "url": "https://api.example.com/x"})


def test_an_outside_host_is_refused() -> None:
    with pytest.raises(ScopeGuardError, match="outside the operation scope"):
        guard(SCOPE, {"target": "10.10.9.9"})


def test_a_second_value_in_a_list_is_checked() -> None:
    with pytest.raises(ScopeGuardError, match="evil.com"):
        guard(SCOPE, {"targets": ["example.com", "evil.com"]})


def test_host_and_port_are_read_as_the_host() -> None:
    guard(SCOPE, {"host": "example.com:8443"})
    with pytest.raises(ScopeGuardError):
        guard(SCOPE, {"host": "evil.com:8443"})


def test_a_network_wider_than_the_scope_is_refused() -> None:
    guard(SCOPE, {"targets": "10.10.0.0/25"})
    with pytest.raises(ScopeGuardError, match="not inside any included network"):
        guard(SCOPE, {"targets": "10.10.0.0/16"})


def test_an_exempt_parameter_is_not_checked() -> None:
    with pytest.raises(ScopeGuardError):
        guard(SCOPE, {"resolver": "https://dns.google/dns-query"})
    guard(SCOPE, {"resolver": "https://dns.google/dns-query"}, exempt=("resolver",))


def test_local_paths_are_not_hosts() -> None:
    guard(SCOPE, {"wordlist": "/usr/share/wordlists/common.txt"})
    guard(SCOPE, {"wordlist": "assets/wordlists/common.txt"})


def test_plain_words_and_numbers_are_not_hosts() -> None:
    guard(SCOPE, {"profile": "fast", "workers": 40, "extensions": ".php,.bak"})


def test_the_message_names_the_parameter() -> None:
    with pytest.raises(ScopeGuardError, match="parameter 'target'"):
        guard(SCOPE, {"target": "8.8.8.8"})


def test_a_free_form_command_is_judged_per_token() -> None:
    """`nmap … <ip>` used to slip through: the whole line is not a bare host."""
    with pytest.raises(ScopeGuardError, match="8.8.8.8"):
        guard(SCOPE, {"command": "nmap -Pn -p- --min-rate 2000 8.8.8.8"})


def test_the_unspecified_address_is_not_a_target() -> None:
    """`::` / `0.0.0.0` are bind-any, not destinations — they must not be refused.

    A bare `::` in a command used to parse as the IPv6 unspecified address and
    trip the guard with "'::' is outside the operation scope".
    """
    guard(SCOPE, {"command": "python3 -m http.server --bind :: 8000"})
    guard(SCOPE, {"command": "nc -lvnp 4444 -s 0.0.0.0"})
    guard(SCOPE, {"command": "ip -6 addr add ::/0 dev eth0"})
    with pytest.raises(ScopeGuardError, match="8.8.8.8"):
        guard(SCOPE, {"command": "bash -lc 'nmap -sV 8.8.8.8'"})
    guard(SCOPE, {"command": "id; uname -a; cat /etc/hosts"})
    guard(SCOPE, {"command": "ssh vm27 'hostname; id'"})


def test_a_shell_semicolon_is_not_part_of_the_url_port() -> None:
    guard(SCOPE, {"command": "curl -s http://10.10.0.5:8443; echo done"})
    guard(SCOPE, {"command": "curl -s https://example.com:8443/path; echo done"})
    with pytest.raises(ScopeGuardError, match="outside.example.org"):
        guard(SCOPE, {"command": "curl -s https://outside.example.org:8443; echo done"})


def test_shell_expansion_after_a_literal_url_port_is_not_the_port() -> None:
    guard(SCOPE, {"command": "curl -s http://10.10.0.5:8443$p"})
    guard(SCOPE, {"command": "curl -s http://10.10.0.5:8443${p}"})
    guard(SCOPE, {"command": "curl -s http://10.10.0.5:8443$p)"})
    with pytest.raises(ScopeGuardError, match="outside.example.org"):
        guard(SCOPE, {"command": "curl -s https://outside.example.org:8443$p"})


def test_xml_and_script_payloads_are_not_network_targets() -> None:
    guard(
        SCOPE,
        {
            "command": (
                "printf '%s' '<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>' "
                "> /tmp/image.svg"
            )
        },
    )
    with pytest.raises(ScopeGuardError, match="8.8.8.8"):
        guard(
            SCOPE,
            {
                "command": (
                    "bash -c 'printf \"%s\" \"<svg xmlns=\\\""
                    "http://www.w3.org/2000/svg\\\"></svg>\"; nmap 8.8.8.8'"
                )
            },
        )


def test_literal_shell_variables_resolve_without_widening_scope() -> None:
    guard(SCOPE, {"command": "IP=10.10.0.5; curl -s https://$IP:8443"})
    guard(SCOPE, {"command": "export IP=10.10.0.5; curl -s http://$IP/"})
    guard(SCOPE, {"command": "IP=10.10.0.5; bash -c 'curl https://$IP:8443'"})
    with pytest.raises(ScopeGuardError, match="outside.example.org"):
        guard(SCOPE, {"command": "IP=outside.example.org; curl -s https://$IP:8443"})
    with pytest.raises(ScopeGuardError, match="same shell call"):
        guard(SCOPE, {"command": "curl -s https://$UNKNOWN:8443"})
    with pytest.raises(ScopeGuardError, match=r":\$PORT"):
        guard(SCOPE, {"command": "curl -s http://10.10.0.5$p"})


def test_key_value_flags_are_judged_by_the_value() -> None:
    with pytest.raises(ScopeGuardError, match="10.9.9.9"):
        guard(SCOPE, {"command": "curl --resolve=example.com:443:10.9.9.9 https://example.com/"})


def test_a_scheme_beats_the_path_heuristic() -> None:
    """`curl https://host` has no path: suffix «.com» used to read as a local file."""
    with pytest.raises(ScopeGuardError, match="outside.example.org"):
        guard(SCOPE, {"command": "curl https://outside.example.org"})


def test_infra_hosts_never_count_as_targets() -> None:
    """Resolvers and the platform API are plumbing: refusing them cost a round."""
    htb = SCOPE.with_infra(infra_hosts("ctf-htb"))
    guard(htb, {"command": "curl -s https://api.hackthebox.com/api/v4/machine/active"})
    guard(htb, {"command": "curl https://labs.hackthebox.com/x"})
    guard(htb, {"command": "dig +short example.com @1.1.1.1"})
    guard(htb, {"command": "curl https://1.1.1.1/dns-query"})
    # the platform list is per mode: HTB infra is not BugBounty infra
    with pytest.raises(ScopeGuardError, match="hackthebox"):
        guard(SCOPE.with_infra(infra_hosts("bug-bounty")),
              {"command": "curl https://api.hackthebox.com/x"})
    # and a real outside target still fails with infra in play
    with pytest.raises(ScopeGuardError, match="outside.example.org"):
        guard(htb, {"command": "curl https://outside.example.org"})


def test_a_file_suffix_beats_the_tld() -> None:
    """`cat app.js` was refused: «.js» is a real TLD and the token looked like one."""
    for command in (
        "cat app.js",
        "grep -n api app.js",
        "head -50 notes.md",
        "cat report.json",
        "sed -n 1p /tmp/c.txt",
        "ls -la exploit.py",
    ):
        guard(SCOPE, {"command": command})
    # a URL is still judged as a URL, whatever its path looks like
    with pytest.raises(ScopeGuardError, match="outside.example.org"):
        guard(SCOPE, {"command": "curl https://outside.example.org/app.js"})


def test_a_lone_file_name_is_not_a_host() -> None:
    """`send.sh` on its own was refused: only a second word skipped it.

    The command is a script the operator wrote next to the operation, so the run
    stopped at the guard and the scope agent called it unrelated.
    """
    for command in ("send.sh", "./send.sh", "run.sh", "linpeas.ps1", "patch.cmd"):
        guard(SCOPE, {"command": command})


def test_a_quoted_tail_still_checks_its_words() -> None:
    """A command in quotes is judged by its words, not dropped over one dot."""
    with pytest.raises(ScopeGuardError, match="8.8.8.8"):
        guard(SCOPE, {"command": "bash -c 'cat app.js; nmap 8.8.8.8'"})
    guard(SCOPE, {"command": "bash -c 'cat app.js; id'"})
