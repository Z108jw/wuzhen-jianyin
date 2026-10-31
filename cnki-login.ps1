$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$profile = Join-Path $root ".runtime\cnki-profile"
$edgeCandidates = @(
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
)
$edge = $edgeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $edge) {
    throw "未找到 Microsoft Edge。"
}

$entityId = $env:CNKI_IDP_ENTITY_ID
if (-not $entityId) {
    throw "请先设置环境变量 CNKI_IDP_ENTITY_ID 为所在机构 CNKI CARSI/Shibboleth 身份提供方实体 ID。"
}

try {
    Invoke-RestMethod "http://127.0.0.1:9223/json/version" -TimeoutSec 3 | Out-Null
    Write-Host "知网授权浏览器已经运行。请在该窗口中完成机构身份验证。"
    exit 0
} catch {}

New-Item -ItemType Directory -Path $profile -Force | Out-Null
$loginUrl = "https://fsso.cnki.net/Shibboleth.sso/Login?entityID=$([uri]::EscapeDataString($entityId))&target=https%3A%2F%2Ffsso.cnki.net%2Fsecure%2Fdefault.aspx"
Start-Process -FilePath $edge -ArgumentList `
    "--remote-debugging-port=9223", `
    "--user-data-dir=$profile", `
    $loginUrl

Write-Host "请在已打开的 Edge 窗口完成机构统一认证和安全验证。"
Write-Host "登录进入知网后保持该浏览器运行；悟真鉴引会自动复用该授权会话。"
