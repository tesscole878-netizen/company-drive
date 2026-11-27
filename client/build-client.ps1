param(
    [Parameter(Mandatory=$true)][string]$RclonePath,
    [Parameter(Mandatory=$true)][string]$RcloneLicense,
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '../releases/windows'),
    [string]$BuildDirectory = (Join-Path $PSScriptRoot '../work/client-build-release')
)
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework C# compiler not found.' }
if (-not (Test-Path -LiteralPath $RclonePath)) { throw 'Official rclone.exe not found.' }
if (-not (Test-Path -LiteralPath $RcloneLicense)) { throw 'Corresponding rclone COPYING license not found.' }
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
$buildRoot = [IO.Path]::GetFullPath($BuildDirectory)
$payloadRoot = Join-Path $buildRoot 'payload'
New-Item -ItemType Directory -Path $outputRoot,$buildRoot,$payloadRoot -Force | Out-Null
$clientExe = Join-Path $payloadRoot 'CompanyDriveClient.exe'
$source = Join-Path $PSScriptRoot 'CompanyDriveClient.cs'
$mountCleanup = Join-Path $PSScriptRoot 'DriveMountCleanup.cs'
$fileChangePopup = Join-Path $PSScriptRoot 'FileChangePopup.cs'
$encryptedLibraries = Join-Path $PSScriptRoot 'EncryptedLibrariesForm.cs'
$manifest = Join-Path $PSScriptRoot 'app.manifest'
$brandIcon = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../drive/static/suyan.ico'))
$brandLogo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../drive/static/suyan-logo.png'))
$companyCertificate = Join-Path $PSScriptRoot 'company-root.cer'
if (-not (Test-Path -LiteralPath $companyCertificate)) { throw 'Company public certificate not found.' }
if (-not (Test-Path -LiteralPath $brandIcon) -or -not (Test-Path -LiteralPath $brandLogo)) { throw 'Suyan brand assets not found.' }
$clientResponse = Join-Path $buildRoot 'client.rsp'
$lines = @('/nologo','/target:winexe','/platform:x64','/optimize+','/codepage:65001','/reference:System.Windows.Forms.dll','/reference:System.Drawing.dll','/reference:System.Net.Http.dll','/reference:System.Net.Http.WebRequest.dll','/reference:System.Web.Extensions.dll','/reference:System.Security.dll',('/win32manifest:"'+$manifest+'"'),('/out:"'+$clientExe+'"'),('"'+$source+'"'))
[IO.File]::WriteAllLines($clientResponse,$lines,[Text.UTF8Encoding]::new($true))
Add-Content -LiteralPath $clientResponse -Value @('/reference:Microsoft.CSharp.dll', ('"'+$mountCleanup+'"'), ('"'+$fileChangePopup+'"'), ('"'+$encryptedLibraries+'"')) -Encoding utf8
Add-Content -LiteralPath $clientResponse -Value @(('/win32icon:"'+$brandIcon+'"'),('/resource:"'+$brandLogo+'",CompanyDrive.Logo'),('/resource:"'+$companyCertificate+'",CompanyDrive.RootCertificate')) -Encoding utf8
& $compiler ('@'+$clientResponse)
if ($LASTEXITCODE -ne 0) { throw 'Client compilation failed.' }
Copy-Item -LiteralPath $RclonePath -Destination (Join-Path $payloadRoot 'rclone.exe')
Copy-Item -LiteralPath $RcloneLicense -Destination (Join-Path $payloadRoot 'RCLONE-LICENSE.txt')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'company.json') -Destination (Join-Path $payloadRoot 'company.json')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $payloadRoot '使用说明.md')
@'
Company Drive Windows Client uses rclone (MIT license).
rclone source: https://github.com/rclone/rclone
rclone releases: https://rclone.org/downloads/
WinFsp is obtained separately by the employee/administrator from its official distribution.
WinFsp project, source and license: https://github.com/winfsp/winfsp
The client validates certificate chains, hostnames and validity periods. Company cloud connections also use the bundled public company root certificate. WinFsp installation uses normal Windows administrator approval.
'@ | Set-Content -LiteralPath (Join-Path $payloadRoot 'THIRD-PARTY-NOTICES.txt') -Encoding utf8
Add-Type -AssemblyName System.IO.Compression.FileSystem
$payloadZip = Join-Path $buildRoot ('payload-'+[Guid]::NewGuid().ToString('N')+'.zip')
[IO.Compression.ZipFile]::CreateFromDirectory($payloadRoot,$payloadZip,[IO.Compression.CompressionLevel]::Optimal,$false)
$setupExe = Join-Path $outputRoot 'CompanyDriveSetup.exe'
$setupResponse = Join-Path $buildRoot 'setup.rsp'
$lines = @('/nologo','/target:winexe','/platform:x64','/optimize+','/codepage:65001','/reference:System.Windows.Forms.dll','/reference:System.Drawing.dll','/reference:System.IO.Compression.dll','/reference:System.IO.Compression.FileSystem.dll','/reference:System.Web.Extensions.dll',('/win32manifest:"'+$manifest+'"'),('/resource:"'+$payloadZip+'",payload.zip'),('/out:"'+$setupExe+'"'),('"'+(Join-Path $PSScriptRoot 'Setup.cs')+'"'))
[IO.File]::WriteAllLines($setupResponse,$lines,[Text.UTF8Encoding]::new($true))
Add-Content -LiteralPath $setupResponse -Value @(('/win32icon:"'+$brandIcon+'"'),('/resource:"'+$brandLogo+'",CompanyDrive.Logo')) -Encoding utf8
Add-Content -LiteralPath $setupResponse -Value @('/reference:Microsoft.CSharp.dll', ('"'+$mountCleanup+'"')) -Encoding utf8
& $compiler ('@'+$setupResponse)
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
Copy-Item -LiteralPath $payloadZip -Destination (Join-Path $outputRoot 'CompanyDrivePortable.zip')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $outputRoot '使用说明.md')
$serverDownloads = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../downloads'))
if (Test-Path -LiteralPath $serverDownloads) { Copy-Item -LiteralPath $setupExe -Destination (Join-Path $serverDownloads 'CompanyDriveSetup.exe') }
$hashes = @('CompanyDriveSetup.exe','CompanyDrivePortable.zip') | ForEach-Object { $h = Get-FileHash -LiteralPath (Join-Path $outputRoot $_) -Algorithm SHA256; $h.Hash.ToLower()+'  '+$_ }
[IO.File]::WriteAllLines((Join-Path $outputRoot 'SHA256SUMS.txt'),$hashes,[Text.UTF8Encoding]::new($false))
Write-Output ('Created installer and portable package at '+$outputRoot)
