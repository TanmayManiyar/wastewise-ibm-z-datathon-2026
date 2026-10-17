# Copy the committed state (HEAD) of this repository to the LinuxONE VM.
# Only tracked files are sent; data/, secrets/ and .venv/ stay local.
# Usage: scripts\sync_to_vm.ps1 [-VmUser linux1] [-VmHost 148.100.79.152]
param(
    [string]$VmUser = "linux1",
    [string]$VmHost = "148.100.79.152",
    [string]$Key = "secrets\vm_key",
    [string]$RemoteDir = "wastewise"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$sshOpts = @("-i", $Key, "-o", "BatchMode=yes", "-o", "UserKnownHostsFile=secrets/known_hosts")
$archive = Join-Path $env:TEMP "wastewise-head.tar"
git archive --format=tar -o $archive HEAD
$rev = git rev-parse --short HEAD
& scp @sshOpts $archive "${VmUser}@${VmHost}:/tmp/wastewise-head.tar"
& ssh @sshOpts "${VmUser}@${VmHost}" "mkdir -p ~/$RemoteDir && tar -xf /tmp/wastewise-head.tar -C ~/$RemoteDir && echo $rev > ~/$RemoteDir/REVISION && rm /tmp/wastewise-head.tar && echo synced $rev"
Remove-Item $archive
