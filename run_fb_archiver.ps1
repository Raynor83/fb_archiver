$ErrorActionPreference = "Stop"

$tokenPath = Join-Path $env:LOCALAPPDATA "fb_archiver\fb_page_token.xml"
if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
    throw "Kein gespeicherter Token gefunden. Zuerst .\save_fb_token.ps1 ausfuehren."
}

$credential = Import-Clixml -LiteralPath $tokenPath
$token = $credential.GetNetworkCredential().Password
if (-not $token) {
    throw "Der gespeicherte Token konnte nicht entschluesselt werden."
}

$hadPreviousToken = Test-Path Env:FB_PAGE_TOKEN
$previousToken = $env:FB_PAGE_TOKEN
$exitCode = 1

try {
    $env:FB_PAGE_TOKEN = $token
    $scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
    $executable = Join-Path $scriptDirectory "dist\fb_archiver.exe"
    $pythonScript = Join-Path $scriptDirectory "fb_archiver.py"

    if (Test-Path -LiteralPath $executable -PathType Leaf) {
        & $executable @args
    }
    else {
        & python $pythonScript @args
    }
    $exitCode = $LASTEXITCODE
}
finally {
    $token = $null
    if ($hadPreviousToken) {
        $env:FB_PAGE_TOKEN = $previousToken
    }
    else {
        Remove-Item Env:FB_PAGE_TOKEN -ErrorAction SilentlyContinue
    }
}

exit $exitCode
