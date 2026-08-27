param(
    [Parameter(Mandatory = $true)]
    [string[]]$Path,
    [string]$MetadataPath = "",
    [switch]$MarkReleaseEligible
)

$ErrorActionPreference = "Stop"

function Find-SignTool {
    $commands = @(
        (Get-Command signtool.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1 -ExpandProperty FullName)
    ) | Where-Object { $_ -and (Test-Path $_) }
    if (-not $commands) {
        throw "SignTool was not found. Install the Windows SDK Build Tools."
    }
    return $commands[0]
}

$signTool = Find-SignTool
$timestampUrl = if ($env:XYNIGO_WINDOWS_TIMESTAMP_URL) {
    $env:XYNIGO_WINDOWS_TIMESTAMP_URL
} else {
    "http://timestamp.digicert.com"
}

$pfxPath = ""
try {
    $signArguments = @()
    if ($env:XYNIGO_WINDOWS_SIGNING_PFX_BASE64) {
        if (-not $env:XYNIGO_WINDOWS_SIGNING_PFX_PASSWORD) {
            throw "The signing certificate password is not configured."
        }
        $temporaryRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
        $pfxPath = Join-Path $temporaryRoot ("xynigo-signing-{0}.pfx" -f [guid]::NewGuid().ToString("N"))
        [IO.File]::WriteAllBytes(
            $pfxPath,
            [Convert]::FromBase64String($env:XYNIGO_WINDOWS_SIGNING_PFX_BASE64)
        )
        $signArguments = @("/f", $pfxPath, "/p", $env:XYNIGO_WINDOWS_SIGNING_PFX_PASSWORD)
    } elseif ($env:XYNIGO_WINDOWS_SIGNING_SUBJECT) {
        $signArguments = @("/n", $env:XYNIGO_WINDOWS_SIGNING_SUBJECT, "/a")
    } else {
        throw "No trusted Authenticode signing identity is configured."
    }

    $resolvedPaths = @()
    foreach ($item in $Path) {
        $resolved = (Resolve-Path $item).Path
        $resolvedPaths += $resolved
        & $signTool sign @signArguments /fd SHA256 /tr $timestampUrl /td SHA256 `
            /d "Xynigo Sourcing" /du "https://xynigo.samforo.icu" $resolved
        if ($LASTEXITCODE -ne 0) {
            throw "Authenticode signing failed for $resolved"
        }
        & $signTool verify /pa /all /v $resolved
        if ($LASTEXITCODE -ne 0) {
            throw "Authenticode verification failed for $resolved"
        }
        $signature = Get-AuthenticodeSignature -FilePath $resolved
        if ($signature.Status -ne "Valid") {
            throw "Windows does not trust the signature for $resolved ($($signature.Status))."
        }
        if (-not $signature.TimeStamperCertificate) {
            throw "The Authenticode signature is missing an RFC 3161 timestamp."
        }
        Write-Host ("SIGNED: {0}" -f (Split-Path $resolved -Leaf))
        Write-Host ("PUBLISHER: {0}" -f $signature.SignerCertificate.Subject)
    }

    if ($MetadataPath) {
        $metadataFile = (Resolve-Path $MetadataPath).Path
        $metadata = Get-Content $metadataFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $installer = $resolvedPaths | Where-Object { (Split-Path $_ -Leaf) -eq $metadata.assetName } | Select-Object -First 1
        if (-not $installer) {
            throw "The signed installer does not match the metadata assetName."
        }
        $signature = Get-AuthenticodeSignature -FilePath $installer
        $metadata.authenticodeSigned = $true
        $metadata.authenticodeTimestamped = [bool]$signature.TimeStamperCertificate
        $metadata.publisherSubject = $signature.SignerCertificate.Subject
        $metadata.publisher = $signature.SignerCertificate.GetNameInfo(
            [Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
            $false
        )
        $metadata.releaseEligible = [bool]$MarkReleaseEligible
        $metadata.sha256 = (Get-FileHash -Algorithm SHA256 -Path $installer).Hash.ToLowerInvariant()
        $metadata.size = (Get-Item $installer).Length
        $metadata | ConvertTo-Json -Depth 8 | Set-Content $metadataFile -Encoding UTF8
    }
} finally {
    if ($pfxPath -and (Test-Path $pfxPath)) {
        Remove-Item -Force $pfxPath
    }
}
