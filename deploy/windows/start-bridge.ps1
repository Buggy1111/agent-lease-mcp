# Spustí WSL a v něm broker + bridge po přihlášení do Windows.
# NEOVĚŘENO z Linuxu — vyzkoušej ručně a pak naplánuj:
#   schtasks /Create /SC ONLOGON /TN agent-lease /TR "powershell -WindowStyle Hidden -File C:\cesta\start-bridge.ps1"
# systemd služby samy WSL instanci naživu neudrží (Microsoft to výslovně uvádí),
# proto tenhle skript drží WSL běžící spuštěným procesem.
param([string]$Distro = "Ubuntu", [string[]]$Agents = @("codex"))
$ErrorActionPreference = "Stop"
wsl -d $Distro -- systemctl --user start agent-lease-broker
foreach ($a in $Agents) { wsl -d $Distro -- systemctl --user start "agent-lease-bridge@$a" }
# držák: bez běžícího procesu WSL po chvíli usne
wsl -d $Distro -- sleep infinity
