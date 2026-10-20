param([int]$Port = 8000)

# Weavlum LAN access fix: in WSL mirrored networking, inbound traffic from
# other devices must pass TWO firewall layers. This script opens both.
# Run it in an ADMIN PowerShell (right click -> "Run as administrator").
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows_firewall.ps1 -Port 8000
#
# NOTE: keep this file pure ASCII - PowerShell 5.1 reads .ps1 as ANSI when
# there is no BOM, and non-ASCII text can turn into mojibake.

$ErrorActionPreference = "Stop"

# Layer 1: standard Windows Firewall inbound rule (all profiles - home LANs
# are often classified as Public, so restricting to Private would be inert).
$ruleName = "Weavlum DND LAN ($Port)"
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
  Write-Host "[ok] Windows Firewall rule already exists: $ruleName"
  Set-NetFirewallRule -DisplayName $ruleName -Profile Any
  Write-Host "[ok] Windows Firewall rule profile ensured: Any"
} else {
  New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
  Write-Host "[ok] Windows Firewall: inbound TCP $Port allowed (all profiles)"
}

# Layer 2: Hyper-V / WSL firewall. WSL mirrored networking is filtered
# separately by the Hyper-V firewall; without a rule inbound LAN traffic
# never reaches the WSL process. Needs Windows 11 22H2+ with WSL 2.0+.
$wslVmCreatorId = "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}"
$hyperName = "Weavlum WSL LAN ($Port)"
try {
  $hyperExisting = Get-NetFirewallHyperVRule -Name $hyperName -ErrorAction SilentlyContinue
  if ($hyperExisting) {
    Write-Host "[ok] WSL (Hyper-V) firewall rule already exists: $hyperName"
  } else {
    New-NetFirewallHyperVRule -Name $hyperName -DisplayName $hyperName -Direction Inbound -VMCreatorId $wslVmCreatorId -Protocol TCP -LocalPorts $Port -Action Allow | Out-Null
    Write-Host "[ok] WSL (Hyper-V) firewall: inbound TCP $Port allowed"
  }
} catch {
  Write-Host "[skip] Hyper-V firewall cmdlet unavailable or failed: $($_.Exception.Message)"
  Write-Host "       (older Windows build - the Windows Firewall rule above may be enough)"
}

Write-Host ""
Write-Host "Done. Other devices should now reach http://<this-PC-LAN-IP>:$Port/"
