"""Provider keys for tools, set once in settings and injected by name.

Tools that talk to third-party services read their key from an environment
variable (subfinder: ``SHODAN_API_KEY``). Asking the model to pass the value as a
parameter meant it had to remember both the source and the vault path; instead the
operator stores the key here and the executor injects it for any env name the tool
declares, unless the call already carried one.
"""

from __future__ import annotations

from dataclasses import dataclass

from samurai_ai.services.vault import VaultService


@dataclass(frozen=True)
class ToolKey:
    """One catalogue entry: the variable a tool reads and where to get the value."""

    env_name: str
    label: str
    tool: str
    url: str
    note: str = ""


#: Every env name any profile declares. The catalogue is what the settings screen
#: lists; a name missing here still works if a tool declares it.
CATALOGUE: tuple[ToolKey, ...] = (
    ToolKey("SHODAN_API_KEY", "Shodan", "intel.shodan",
            "https://account.shodan.io", "host lookup and domain DNS map"),
    ToolKey("CENSYS_API_KEY", "Censys (subfinder)", "recon.subfinder-passive",
            "https://accounts.censys.io/settings/personal-access-tokens",
            "passive subdomains via subfinder"),
    ToolKey("CENSYS_API_TOKEN", "Censys (tool)", "intel.censys",
            "https://accounts.censys.io/settings/personal-access-tokens",
            "host lookup and certificate search"),
    ToolKey("CENSYS_ORG_ID", "Censys organization id", "intel.censys",
            "https://accounts.censys.io/settings/personal-access-tokens",
            "optional, for paid plans"),
    ToolKey("VIRUSTOTAL_API_KEY", "VirusTotal", "recon.subfinder-passive",
            "https://www.virustotal.com/gui/my-apikey", "passive subdomains"),
    ToolKey("SECURITYTRAILS_API_KEY", "SecurityTrails", "recon.subfinder-passive",
            "https://securitytrails.com/app/account/credentials", "passive subdomains"),
    ToolKey("GITHUB_API_KEY", "GitHub", "recon.subfinder-passive",
            "https://github.com/settings/tokens", "passive subdomains"),
    ToolKey("CHAOS_API_KEY", "Chaos", "recon.subfinder-passive",
            "https://cloud.projectdiscovery.io", "passive subdomains"),
    ToolKey("URLSCAN_API_KEY", "urlscan.io", "recon.subfinder-passive",
            "https://urlscan.io/user/signup", "passive subdomains"),
    ToolKey("FOFA_API_KEY", "FOFA", "recon.subfinder-passive",
            "https://fofa.info", "passive subdomains"),
    ToolKey("ZOOMEYEAPI_API_KEY", "ZoomEye", "recon.subfinder-passive",
            "https://www.zoomeye.org/profile", "passive subdomains"),
    ToolKey("INTELX_API_KEY", "Intelligence X", "recon.subfinder-passive",
            "https://intelx.io/account", "passive subdomains"),
    ToolKey("QUAKE_API_KEY", "Quake", "recon.subfinder-passive",
            "https://quake.360.net", "passive subdomains"),
    ToolKey("C99_API_KEY", "C99", "recon.subfinder-passive",
            "https://subdomainfinder.c99.nl", "passive subdomains"),
    ToolKey("CERTSPOTTER_API_KEY", "CertSpotter", "recon.subfinder-passive",
            "https://sslmate.com/certspotter/api_keys", "passive subdomains"),
    ToolKey("DNSDUMPSTER_API_KEY", "DNSDumpster", "recon.subfinder-passive",
            "https://dnsdumpster.com", "passive subdomains"),
    ToolKey("DIGITALYAMA_API_KEY", "DigitalYama", "recon.subfinder-passive",
            "https://api.digitalyama.com", "passive subdomains"),
    ToolKey("WINDVANE_API_KEY", "Windvane", "recon.subfinder-passive",
            "https://windvane.lichoin.com", "passive subdomains"),
    ToolKey("WPSCAN_API_KEY", "WPScan", "scan.wpscan",
            "https://wpscan.com/profile", "WordPress scan (covers vulnerabilities)"),
)


def vault_name(env_name: str) -> str:
    """Env names are upper case, vault names are lower case: one explicit mapping."""
    return env_name.lower()


def set_key(vault: VaultService, env_name: str, value: str, by: str = "operator") -> None:
    """Store or replace a key; an empty value clears it."""
    name = vault_name(env_name)
    if not value:
        vault.remove(name)
        return
    if vault.exists(name):
        vault.set(name, value)
        return
    vault.add(name, value, created_by=by)


def key_state(vault: VaultService | None, env_name: str) -> str:
    """`set` / `empty` — what the settings screen shows, never the value."""
    if vault is None:
        return "empty"
    return "set" if vault.exists(vault_name(env_name)) else "empty"


def key_for(env_name: str) -> ToolKey | None:
    for entry in CATALOGUE:
        if entry.env_name == env_name:
            return entry
    return None


def stored_keys(vault: VaultService | None) -> dict[str, str]:
    """Every catalogue key that has a value in the vault (global scope only)."""
    if vault is None:
        return {}
    found: dict[str, str] = {}
    for entry in CATALOGUE:
        if vault.exists(vault_name(entry.env_name)):
            found[entry.env_name] = vault.get(vault_name(entry.env_name))
    return found


def inject(
    env_names: tuple[str, ...], provided: dict[str, str], vault: VaultService | None
) -> dict[str, str]:
    """Fill declared env names from the vault, leaving what the call carried."""
    if vault is None:
        return {}
    injected: dict[str, str] = {}
    for name in env_names:
        if name in provided:
            continue
        if not vault.exists(vault_name(name)):
            continue
        injected[name] = vault.get(vault_name(name))
    return injected


__all__ = [
    "CATALOGUE",
    "ToolKey",
    "inject",
    "key_for",
    "key_state",
    "set_key",
    "stored_keys",
    "vault_name",
]
