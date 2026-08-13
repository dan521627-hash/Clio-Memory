#!/usr/bin/env sh
set -eu

command -v docker >/dev/null 2>&1 || {
  echo "Docker is not installed." >&2
  exit 1
}

[ -f config.yaml ] || cp config.example.yaml config.yaml
if [ ! -f .env ]; then
  generate_secret() {
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -hex 16
    elif command -v uuidgen >/dev/null 2>&1; then
      uuidgen | tr -d '-'
    elif [ -r /proc/sys/kernel/random/uuid ]; then
      tr -d '-' < /proc/sys/kernel/random/uuid
    else
      echo "Cannot generate a secure secret. Install openssl and retry." >&2
      exit 1
    fi
  }
  password="$(generate_secret)"
  seal="$(generate_secret)"
  cat > .env <<EOF
OMBRE_API_KEY=
OMBRE_RESPONSE_SEAL=$seal
CLIO_MANAGER_PASSWORD=$password
OMBRE_BARK_BASE_URL=https://api.day.app
OMBRE_BARK_DEVICE_KEY=
CLIO_DATA_DIR=./data
CLIO_MODEL_DIR=./models
CLIO_EXPORT_DIR=./exports
EOF
  chmod 600 .env
  echo "Manager password: $password"
  echo "Keep this password private. It is stored only in your local .env file."
fi

mkdir -p data models exports private
docker compose up -d --build
echo "MCP:     http://127.0.0.1:18001/mcp"
echo "Manager: http://127.0.0.1:8787"
