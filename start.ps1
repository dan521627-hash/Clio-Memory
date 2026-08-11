$ErrorActionPreference = "Stop"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed. Install Docker Desktop first."
}

if (-not (Test-Path ".\config.yaml")) {
    Copy-Item ".\config.example.yaml" ".\config.yaml"
}

if (-not (Test-Path ".\.env")) {
    $password = [Guid]::NewGuid().ToString("N")
    $seal = [Guid]::NewGuid().ToString("N")
    @"
OMBRE_API_KEY=
OMBRE_RESPONSE_SEAL=$seal
CLIO_MANAGER_PASSWORD=$password
OMBRE_BARK_BASE_URL=https://api.day.app
OMBRE_BARK_DEVICE_KEY=
CLIO_DATA_DIR=./data
CLIO_MODEL_DIR=./models
CLIO_EXPORT_DIR=./exports
"@ | Set-Content -Encoding utf8 ".\.env"
    Write-Host "Manager password: $password"
    Write-Host "Keep this password private. It is stored only in your local .env file."
}

New-Item -ItemType Directory -Force data, models, exports, private | Out-Null
docker compose up -d --build
Write-Host "MCP:     http://127.0.0.1:18001/mcp"
Write-Host "Manager: http://127.0.0.1:8787"

