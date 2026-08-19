[CmdletBinding()]
param(
    [string]$PageId = "168701373143130"
)

$ErrorActionPreference = "Stop"

if ($PageId -notmatch "^\d+$") {
    throw "PageId muss eine numerische Facebook-Seiten-ID sein."
}

$tokenDirectory = Join-Path $env:LOCALAPPDATA "fb_archiver"
$tokenPath = Join-Path $tokenDirectory "fb_page_token.xml"

$secureToken = Read-Host "Facebook User oder Page Access Token" -AsSecureString
if ($secureToken.Length -lt 20) {
    throw "Der eingegebene Token ist ungewoehnlich kurz. Es wurde nichts gespeichert."
}

$inputCredential = [System.Management.Automation.PSCredential]::new(
    "INPUT_TOKEN",
    $secureToken
)
$inputToken = $inputCredential.GetNetworkCredential().Password
$pageToken = $inputToken

try {
    try {
        Invoke-RestMethod `
            -Uri "https://graph.facebook.com/v26.0/$PageId/posts?fields=id&limit=1" `
            -Headers @{ Authorization = "Bearer $inputToken" } `
            -Method Get `
            -TimeoutSec 30 `
            -ErrorAction Stop | Out-Null
    }
    catch {
        $page = Invoke-RestMethod `
            -Uri "https://graph.facebook.com/v26.0/$PageId`?fields=id,name,access_token" `
            -Headers @{ Authorization = "Bearer $inputToken" } `
            -Method Get `
            -TimeoutSec 30 `
            -ErrorAction Stop
        if (-not $page.access_token) {
            throw "Meta hat keinen Page Access Token fuer Seite $PageId geliefert."
        }
        $pageToken = $page.access_token

        Invoke-RestMethod `
            -Uri "https://graph.facebook.com/v26.0/$PageId/posts?fields=id&limit=1" `
            -Headers @{ Authorization = "Bearer $pageToken" } `
            -Method Get `
            -TimeoutSec 30 `
            -ErrorAction Stop | Out-Null
    }

    $securePageToken = ConvertTo-SecureString $pageToken -AsPlainText -Force
    $credential = [System.Management.Automation.PSCredential]::new(
        "FB_PAGE_TOKEN",
        $securePageToken
    )
    New-Item -ItemType Directory -Path $tokenDirectory -Force | Out-Null
    $credential | Export-Clixml -LiteralPath $tokenPath -Force
}
finally {
    $inputToken = $null
    $pageToken = $null
    $securePageToken = $null
}

Write-Host "Token verschluesselt gespeichert: $tokenPath"
Write-Host "Page-Posts fuer Seite $PageId wurden erfolgreich validiert."
Write-Host "Er kann nur mit diesem Windows-Benutzer auf diesem Computer entschluesselt werden."
