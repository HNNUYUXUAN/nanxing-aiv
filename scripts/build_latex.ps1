param(
    [string]$Source = 'templates/CUMCMThesis/example.tex',
    [string]$OutputDirectory = 'build/cumcm-template'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$texBin = Join-Path $env:APPDATA 'TinyTeX/bin/windows'
if (-not (Test-Path (Join-Path $texBin 'latexmk.exe'))) {
    throw "TinyTeX latexmk not found: $texBin"
}
$sourcePath = if ([IO.Path]::IsPathRooted($Source)) { $Source } else { Join-Path $projectRoot $Source }
$sourcePath = (Resolve-Path -LiteralPath $sourcePath).Path
$outputPath = if ([IO.Path]::IsPathRooted($OutputDirectory)) { $OutputDirectory } else { Join-Path $projectRoot $OutputDirectory }
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
$outputPath = (Resolve-Path -LiteralPath $outputPath).Path
$savedPath = $env:PATH
$savedLocale = @{}
foreach ($key in @('LC_ALL', 'LC_CTYPE', 'LANG')) {
    $savedLocale[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
    [Environment]::SetEnvironmentVariable($key, 'C', 'Process')
}
try {
    $env:PATH = "$texBin;$savedPath"
    Push-Location (Split-Path $sourcePath -Parent)
    try {
        & (Join-Path $texBin 'latexmk.exe') -g -xelatex -interaction=nonstopmode -halt-on-error -file-line-error -synctex=1 "-outdir=$outputPath" (Split-Path $sourcePath -Leaf)
        if ($LASTEXITCODE -ne 0) { throw "LaTeX compilation failed; see logs in $outputPath" }
    } finally { Pop-Location }
} finally {
    $env:PATH = $savedPath
    foreach ($key in $savedLocale.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedLocale[$key], 'Process')
    }
}
Write-Host "PDF: $(Join-Path $outputPath ([IO.Path]::GetFileNameWithoutExtension($sourcePath) + '.pdf'))"
