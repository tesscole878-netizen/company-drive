param(
    [ValidatePattern('^[A-Za-z0-9_-]+$')][string]$Remote = 'company',
    [ValidatePattern('^[D-Zd-z]$')][string]$Letter = 'Z'
)
$ErrorActionPreference = 'Stop'
$rcloneCommand = Get-Command rclone -ErrorAction SilentlyContinue
if (-not $rcloneCommand) { throw 'Install rclone from https://rclone.org/downloads/ and add it to PATH first.' }
$driveTarget = $Letter.ToUpper() + ':'
if (Test-Path -LiteralPath ($driveTarget + '\')) { throw "$driveTarget is already in use. Choose another letter." }
$configured = & $rcloneCommand.Source listremotes
if ($LASTEXITCODE -ne 0 -or ($Remote + ':') -notin $configured) {
    throw "Run 'rclone config' first and create a WebDAV remote named '$Remote'. See README.md."
}
Write-Host "Mounting Company Drive at $driveTarget. Keep this window open."
Write-Host 'After saving files, wait until uploads finish before pressing Ctrl+C.'
& $rcloneCommand.Source mount ($Remote + ':') $driveTarget --vfs-cache-mode full --network-mode --dir-cache-time 30s --vfs-cache-max-size 20G --vfs-write-back 3s
exit $LASTEXITCODE
