# Copy the committed state (HEAD) of this repository to the LinuxONE VM.
#  * ~/wastewise           the deployment the services run from (plus its .venv, data/, models/, secrets/)
#  * ~/shared/wastewise    a browsable copy of the source for JupyterLab (the Jupyter container only
#                          mounts ~/shared). Replaced on every sync - edit code in the repository, not there.
# Only tracked files are sent; data/, secrets/ and .venv/ are never copied.
# Usage: scripts\sync_to_vm.ps1 [-VmUser linux1] [-VmHost 148.100.79.152]
param(
    [string]$VmUser = "linux1",
    [string]$VmHost = "148.100.79.152",
    [string]$Key = "secrets\vm_key",
    [string]$RemoteDir = "wastewise",
    [string]$SharedDir = "shared/wastewise"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$sshOpts = @("-i", $Key, "-o", "BatchMode=yes", "-o", "UserKnownHostsFile=secrets/known_hosts")
$archive = Join-Path $env:TEMP "wastewise-head.tar"
git archive --format=tar -o $archive HEAD
$rev = git rev-parse --short HEAD
$note = "Read-only copy of the WasteWise source (git $rev), refreshed on every sync. The app runs from ~/wastewise on the VM; edit the code in the repository, not here."
& scp @sshOpts $archive "${VmUser}@${VmHost}:/tmp/wastewise-head.tar"
& ssh @sshOpts "${VmUser}@${VmHost}" "mkdir -p ~/$RemoteDir && tar -xf /tmp/wastewise-head.tar -C ~/$RemoteDir && echo $rev > ~/$RemoteDir/REVISION && rm -rf ~/$SharedDir && mkdir -p ~/$SharedDir && tar -xf /tmp/wastewise-head.tar -C ~/$SharedDir && echo '$note' > ~/$SharedDir/ABOUT_THIS_COPY.txt && rm /tmp/wastewise-head.tar && echo synced $rev"
Remove-Item $archive
