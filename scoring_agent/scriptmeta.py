"""Read a generated scoring `.ps1` and work out which VM to select in the portal.

Rules (from how the scoring scripts are built):
- Single-VM lab  -> the `New-PSSession` / `.Add()` lines are COMMENTED OUT. The VM to select
  is the value of `$RemoteServer` in the header.
- Multi-VM lab   -> those lines are UNCOMMENTED (one block per target VM). The script runs on
  the Domain Controller, so the portal VM to select is the DC (not the remote targets that
  the `$RemoteServer` lines name).
- Cloud / AWS headers have no `$RemoteServer`; the VM must be supplied another way.
"""
from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass
class ScriptMeta:
    remote_servers: list           # every $RemoteServer value found
    opens_sessions: bool           # any UNcommented $script:RemoteSessions.Add(
    is_cloud: bool                 # cloud/aws header (Connect-AzAccount / Initialize-AWS)

    @property
    def is_multi_vm(self) -> bool:
        return self.opens_sessions

    def vm_to_select(self, dc_override: str | None = None):
        """Return (vm_name, reason). vm_name is None when it can't be decided safely."""
        if dc_override:
            return dc_override, "explicit --dc / manifest value"
        if self.is_cloud:
            return None, "cloud/AWS script has no VM to select"
        if self.is_multi_vm:
            dc = _guess_dc(self.remote_servers)
            if dc:
                return dc, "multi-VM: picked the DC-looking server from the header"
            return None, ("multi-VM: could not identify the DC from the header "
                          "(no server name contains 'DC') - supply --dc")
        # single VM
        if len(self.remote_servers) == 1:
            return self.remote_servers[0], "single-VM: the one $RemoteServer in the header"
        if self.remote_servers:
            return self.remote_servers[0], "single-VM: first $RemoteServer in the header"
        return None, "no $RemoteServer found in the header"


def _guess_dc(servers: list):
    for s in servers:
        if "DC" in s.upper():
            return s
    return None


def parse_script(path: str) -> ScriptMeta:
    text = open(path, encoding="utf-8-sig").read()
    header = text.split("$scoretests", 1)[0]  # only the connection header matters

    remote_servers = re.findall(r"\$RemoteServer\d*\s*=\s*'([^']+)'", header)

    opens = False
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"\$script:RemoteSessions\.Add\(", stripped):
            opens = True
            break

    is_cloud = bool(re.search(r"Connect-AzAccount|Initialize-AWSDefaultConfiguration", header))

    return ScriptMeta(remote_servers=remote_servers, opens_sessions=opens, is_cloud=is_cloud)
