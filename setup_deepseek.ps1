param(
    [string]$Model = 'deepseek-flash'
)

# Test the official JSON API before committing process environment settings.
$ErrorActionPreference = 'Stop'
$apiBase = 'https://api.deepseek.com'
$apiKey = $env:AFL_API_KEY
$failureMessage = $null
try {
    if ([string]::IsNullOrWhiteSpace($Model)) {
        throw 'Model must not be empty.'
    }
    $Model = $Model.Trim()
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        $secret = Read-Host 'Paste your DeepSeek API key (input hidden)' -AsSecureString
        $apiKey = ([System.Net.NetworkCredential]::new('', $secret)).Password
    }
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw 'Empty API key. No request was sent.'
    }
    $apiKey = $apiKey.Trim()
    Write-Host 'Testing the DeepSeek JSON API (one small paid request)...'
    $body = @{
        model = $Model
        messages = @(
            @{role = 'system'; content = 'Return only valid JSON. Example: {"ok":true}'},
            @{role = 'user'; content = 'Reply with the JSON object {"ok":true}.'}
        )
        response_format = @{type = 'json_object'}
        thinking = @{type = 'disabled'}
        stream = $false
        max_tokens = 128
    } | ConvertTo-Json -Depth 6 -Compress
    $reply = Invoke-RestMethod -Method Post -Uri "$apiBase/chat/completions" `
        -Headers @{Authorization = "Bearer $apiKey"} `
        -ContentType 'application/json; charset=utf-8' `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 120
    if ($null -eq $reply.choices -or $reply.choices.Count -lt 1) {
        throw 'The response has no choices.'
    }
    $choice = $reply.choices[0]
    if ($choice.finish_reason -ne 'stop') {
        throw 'The response did not finish normally.'
    }
    $content = $choice.message.content
    if ($content -isnot [string] -or [string]::IsNullOrWhiteSpace($content)) {
        throw 'The response has no text.'
    }
    $parsed = $content | ConvertFrom-Json -ErrorAction Stop
    if ($null -eq $parsed -or $parsed -isnot [pscustomobject] `
            -or $parsed.ok -isnot [bool] -or $parsed.ok -ne $true) {
        throw 'The response is not the expected JSON object.'
    }
    $env:AFL_PROVIDER = 'compatible'
    $env:AFL_API_BASE_URL = $apiBase
    $env:AFL_MODEL = $Model
    $env:AFL_JSON_MODE = 'true'
    $env:AFL_API_KEY = $apiKey
    Write-Host 'SUCCESS: DeepSeek returned the expected JSON object.'
    Write-Host 'Configuration is ready in this PowerShell window. Run: .\start.ps1'
} catch {
    $status = $null
    if ($_.Exception.Response) {
        try { $status = [int]$_.Exception.Response.StatusCode } catch {}
    }
    if ($null -ne $status) {
        $hints = @{
            400 = 'Check the model and request parameters.'
            401 = 'Check the API key.'
            402 = 'Check API credits.'
            403 = 'Check model access.'
            404 = 'Check the model name.'
            422 = 'Check the model and request parameters.'
            429 = 'Rate limit reached; retry later.'
        }
        $hint = $hints[$status]
        if (-not $hint) { $hint = 'Check service availability and retry later.' }
        $failureMessage = "DeepSeek HTTP $status. $hint Configuration was not changed."
    } else {
        $failureMessage = 'DeepSeek JSON API test failed. Check the key, network, proxy and response format. Configuration was not changed.'
    }
} finally {
    $apiKey = $null
    Remove-Variable secret -ErrorAction SilentlyContinue
}
if ($failureMessage) {
    throw $failureMessage
}
