$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$tunnel = (Get-Command cloudflared.exe -ErrorAction SilentlyContinue).Source
if (-not $tunnel) {
    $tunnel = Join-Path $root "tools\cloudflared.exe"
}

$runtime = Join-Path $root ".runtime"
$publicFile = Join-Path $runtime "public_url.txt"
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
Remove-Item -LiteralPath $publicFile -Force -ErrorAction SilentlyContinue

if (-not (Test-Path -LiteralPath $python)) {
    throw "请先运行 .\run.ps1 完成环境安装。"
}

try {
    Invoke-RestMethod "http://127.0.0.1:8000/api/health" | Out-Null
} catch {
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $root "run.ps1") `
        -WindowStyle Hidden
    $ready = $false
    1..30 | ForEach-Object {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-RestMethod "http://127.0.0.1:8000/api/health" | Out-Null
            $ready = $true
            return
        } catch {}
    }
    if (-not $ready) {
        throw "应用未能在 15 秒内启动，请先运行 .\run.ps1 查看错误。"
    }
}

if (-not (Test-Path -LiteralPath $tunnel)) {
    $tunnelDir = Split-Path -Parent $tunnel
    $download = "$tunnel.download"
    New-Item -ItemType Directory -Path $tunnelDir -Force | Out-Null
    Write-Host "正在下载 Cloudflare Tunnel..."
    Invoke-WebRequest `
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
        -OutFile $download
    Move-Item -LiteralPath $download -Destination $tunnel
}

$stdoutLog = Join-Path $runtime "cloudflared.out.log"
$stderrLog = Join-Path $runtime "cloudflared.err.log"
Remove-Item -LiteralPath $stdoutLog, $stderrLog -Force -ErrorAction SilentlyContinue

$tunnelProcess = $null
$publicUrl = $null
try {
    Write-Host "正在建立临时公网地址，请稍候..."
    $tunnelProcess = Start-Process -FilePath $tunnel `
        -ArgumentList "tunnel", "--url", "http://127.0.0.1:8000", "--no-autoupdate" `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru `
        -WindowStyle Hidden

    1..60 | ForEach-Object {
        Start-Sleep -Milliseconds 500
        $logText = @(
            Get-Content -LiteralPath $stdoutLog -Raw -ErrorAction SilentlyContinue
            Get-Content -LiteralPath $stderrLog -Raw -ErrorAction SilentlyContinue
        ) -join "`n"
        $match = [regex]::Match($logText, "https://[a-z0-9-]+\.trycloudflare\.com")
        if ($match.Success) {
            $script:publicUrl = $match.Value
            return
        }
    }

    if (-not $publicUrl) {
        $detail = Get-Content -LiteralPath $stderrLog -Raw -ErrorAction SilentlyContinue
        throw "未能建立公网隧道。$detail"
    }

    Set-Content -LiteralPath $publicFile -Value $publicUrl -Encoding utf8NoBOM
    Write-Host ""
    Write-Host "公网应用地址：$publicUrl" -ForegroundColor Green
    Write-Host "已写入应用配置。现在点击页面右上角“分享应用”即可复制链接或展示二维码。"
    Write-Host "保留此窗口；关闭窗口或电脑关机后，该地址失效。"
    Wait-Process -Id $tunnelProcess.Id
}
finally {
    Remove-Item -LiteralPath $publicFile -Force -ErrorAction SilentlyContinue
    if ($tunnelProcess -and -not $tunnelProcess.HasExited) {
        Stop-Process -Id $tunnelProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
