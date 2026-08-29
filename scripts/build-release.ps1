param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$componentRelative = "custom_components/xiaomi_lock_cloud_backup"
$componentRoot = Join-Path $projectRoot $componentRelative
$versionFile = (Get-Content -LiteralPath (Join-Path $projectRoot "VERSION") -Raw).Trim()
if (-not $Version) {
    $Version = $versionFile
}
if ($Version -notmatch '^V\d+\.\d+\.\d+$' -or $Version -ne $versionFile) {
    throw "Release version does not match VERSION"
}

$manifest = Get-Content -LiteralPath (Join-Path $componentRoot "manifest.json") -Raw | ConvertFrom-Json
if ("V$($manifest.version)" -ne $Version) {
    throw "Release version does not match manifest"
}

$artifactRoot = Join-Path $projectRoot "artifacts"
$releaseRoot = Join-Path $artifactRoot $Version
$zipName = "xiaomi-lock-cloud-video-backup-$Version.zip"
$zipTarget = Join-Path $releaseRoot $zipName
$hashTarget = Join-Path $releaseRoot "SHA256SUMS.txt"
if (Test-Path -LiteralPath $releaseRoot) {
    throw "Release directory already exists; refusing to overwrite it"
}

$workRoot = Join-Path $artifactRoot (".build-" + [guid]::NewGuid().ToString("N"))
$stageRoot = Join-Path $workRoot "stage"
$zipTemporary = Join-Path $workRoot $zipName

function Remove-VerifiedWorkRoot {
    if (-not (Test-Path -LiteralPath $workRoot)) {
        return
    }
    $resolvedArtifact = [System.IO.Path]::GetFullPath($artifactRoot)
    $resolvedWork = [System.IO.Path]::GetFullPath($workRoot)
    if (-not $resolvedWork.StartsWith($resolvedArtifact + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Temporary release directory escaped artifacts root"
    }
    Remove-Item -LiteralPath $resolvedWork -Recurse -Force
}

try {
    $componentFiles = @(
        & git -C $projectRoot ls-files --cached --others --exclude-standard -- $componentRelative |
            Sort-Object
    )
    if ($LASTEXITCODE -ne 0 -or $componentFiles.Count -lt 1) {
        throw "Unable to enumerate release source files"
    }
    foreach ($relativeSource in $componentFiles) {
        $source = Join-Path $projectRoot $relativeSource
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Release source file is missing"
        }
        $destination = Join-Path $stageRoot $relativeSource
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination
    }

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zipStream = $null
    $zipArchive = $null
    try {
        $zipStream = [System.IO.File]::Open(
            $zipTemporary,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        $zipArchive = [System.IO.Compression.ZipArchive]::new(
            $zipStream,
            [System.IO.Compression.ZipArchiveMode]::Create,
            $false
        )
        foreach ($file in Get-ChildItem -LiteralPath $stageRoot -Recurse -File) {
            $entryName = $file.FullName.Substring($stageRoot.Length).TrimStart([char[]]"\/").Replace("\", "/")
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zipArchive,
                $file.FullName,
                $entryName,
                [System.IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    }
    finally {
        if ($null -ne $zipArchive) {
            $zipArchive.Dispose()
        }
        if ($null -ne $zipStream) {
            $zipStream.Dispose()
        }
    }

    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipTemporary)
    try {
        $entries = @($archive.Entries | Where-Object { -not [string]::IsNullOrEmpty($_.Name) })
        foreach ($entry in $entries) {
            if ($entry.FullName.Contains("\") -or $entry.FullName.StartsWith("/") -or $entry.FullName.Split("/") -contains "..") {
                throw "Release archive contains an unsafe entry"
            }
        }
    }
    finally {
        $archive.Dispose()
    }

    $extractRoot = Join-Path $workRoot "extract"
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zipTemporary, $extractRoot)
    $sourceFiles = @($componentFiles | ForEach-Object { Get-Item -LiteralPath (Join-Path $projectRoot $_) })
    $extractedFiles = @(Get-ChildItem -LiteralPath (Join-Path $extractRoot $componentRelative) -Recurse -File)
    if ($sourceFiles.Count -ne $extractedFiles.Count -or $sourceFiles.Count -lt 1) {
        throw "Release file count does not match source"
    }
    foreach ($source in $sourceFiles) {
        $relative = $source.FullName.Substring($componentRoot.Length).TrimStart([char[]]"\/")
        $extracted = Join-Path (Join-Path $extractRoot $componentRelative) $relative
        if (-not (Test-Path -LiteralPath $extracted)) {
            throw "Release archive is missing a source file"
        }
        $sourceHash = (Get-FileHash -LiteralPath $source.FullName -Algorithm SHA256).Hash
        $extractedHash = (Get-FileHash -LiteralPath $extracted -Algorithm SHA256).Hash
        if ($sourceHash -ne $extractedHash) {
            throw "Release archive content differs from source"
        }
    }

    New-Item -ItemType Directory -Path $releaseRoot | Out-Null
    Move-Item -LiteralPath $zipTemporary -Destination $zipTarget
    $hash = (Get-FileHash -LiteralPath $zipTarget -Algorithm SHA256).Hash
    [System.IO.File]::WriteAllText($hashTarget, "$hash  $zipName`n", [System.Text.UTF8Encoding]::new($false))
    Write-Output "RELEASE_PASS version=$Version files=$($sourceFiles.Count) sha256=$hash"
}
finally {
    Remove-VerifiedWorkRoot
}
