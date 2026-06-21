# Profile shim — dot-sourced by the PowerShell profile so `ironclad` + `ironclad-doctor` resolve to the
# installed launcher copies. $PSScriptRoot is the dir holding these scripts (the clone's install/ for an
# in-place install, or the runtime dir for a copied runtime). Pure routing — the logic is in ironclad.ps1.
$script:IroncladRC = $PSScriptRoot
function global:ironclad        { & (Join-Path $script:IroncladRC 'ironclad.ps1')        @args }
function global:ironclad-doctor { & (Join-Path $script:IroncladRC 'ironclad-doctor.ps1') @args }
