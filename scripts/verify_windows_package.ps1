$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$exe = Join-Path $projectRoot 'dist-app\ChurchTranslator\ChurchTranslator.exe'
$tempRoot = Join-Path $projectRoot 'build\test-temp'
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$previousScale = $env:QT_SCALE_FACTOR
try {
    $env:TEMP = $tempRoot
    $env:TMP = $tempRoot
    foreach ($scale in @('1', '1.5', '2')) {
        $env:QT_SCALE_FACTOR = $scale
        $tag = $scale.Replace('.', '')
        $report = Join-Path $projectRoot "build\improvement-exe-dpi$tag.json"
        $process = Start-Process -FilePath $exe -ArgumentList @('--smoke-test', $report) -WindowStyle Hidden -PassThru
        if (-not $process.WaitForExit(45000)) { throw "Packaged smoke timed out at $scale" }
        $process.Refresh()
        $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
        if ($process.ExitCode -ne 0 -or -not $result.ok) { throw "Packaged smoke failed at $scale : $report" }
        Write-Output "Scale $scale passed: $report"
    }
} finally {
    $env:QT_SCALE_FACTOR = $previousScale
}
