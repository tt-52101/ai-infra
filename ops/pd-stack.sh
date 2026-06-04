#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/compose"
ENV_FILE="${COMPOSE_DIR}/.env"

COMPOSE_FILES=(
  "docker-compose.yml"
  "docker-compose.lmcache.yml"
  "docker-compose.prefill.yml"
  "docker-compose.decode.yml"
  "docker-compose.gateway.yml"
)

usage() {
  cat <<'USAGE'
Usage:
  ops/pd-stack.sh <command> [target] [extra docker compose args...]

Commands:
  config          Render merged Compose config
  up              Start stack or target service group
  down            Stop full stack
  restart         Restart stack or target service group
  ps              Show service status
  logs            Tail logs for stack or target service group
  health          Check gateway health endpoint
  verify          Run MVP TTFT verification script
  doctor          Verify rendered Compose critical settings
  repair          Doctor + force recreate stack or target service group
  pull            Pull images
  build           Build gateway image

Targets:
  all             lmcache + prefill + decode + gateway (default)
  cache           lmcache-server
  prefill         vllm-prefill
  decode          vllm-decode
  gateway         gateway

Examples:
  ops/pd-stack.sh config
  ops/pd-stack.sh up
  ops/pd-stack.sh doctor
  ops/pd-stack.sh repair prefill
  ops/pd-stack.sh logs gateway
  ops/pd-stack.sh restart decode
  GATEWAY_API_KEY=sk-real ops/pd-stack.sh verify
USAGE
}

compose_args=()
for file in "${COMPOSE_FILES[@]}"; do
  compose_args+=("-f" "${COMPOSE_DIR}/${file}")
done

if [[ -f "${ENV_FILE}" ]]; then
  compose_args+=("--env-file" "${ENV_FILE}")
fi

dc() {
  docker compose "${compose_args[@]}" "$@"
}

check_rendered_config() {
  local rendered="$1"
  local failed=0

  require_present() {
    local needle="$1"
    if [[ "${rendered}" != *"${needle}"* ]]; then
      echo "ERROR: rendered Compose missing required setting: ${needle}" >&2
      failed=1
    fi
  }

  require_absent() {
    local needle="$1"
    if [[ "${rendered}" == *"${needle}"* ]]; then
      echo "ERROR: rendered Compose still contains stale setting: ${needle}" >&2
      failed=1
    fi
  }

  require_present "lmcache/vllm-openai:v0.4.5-cu129"
  require_present "lmcache/standalone:v0.4.5-cu129"
  require_present "/model"
  require_present "--disable-custom-all-reduce"
  require_present "NCCL_DEBUG"

  require_absent "latest-nightly"
  require_absent "standalone:nightly"
  require_absent "--model /model"
  require_absent "lmcache/lmcache-server"

  if [[ "${failed}" -ne 0 ]]; then
    echo "ERROR: rendered Compose is stale; fix compose/.env or pull latest repo files before starting containers." >&2
    return 1
  fi

  echo "OK: rendered Compose uses fixed LMCache/vLLM images, positional /model, disabled custom all-reduce, and NCCL_DEBUG."
}

doctor() {
  local rendered
  rendered="$(dc config "$@")"
  check_rendered_config "${rendered}"
}

services_for_target() {
  local target="${1:-all}"
  case "${target}" in
    all) echo "lmcache-server vllm-prefill vllm-decode gateway" ;;
    cache|lmcache) echo "lmcache-server" ;;
    prefill) echo "vllm-prefill" ;;
    decode) echo "vllm-decode" ;;
    gateway) echo "gateway" ;;
    *) echo "Unknown target: ${target}" >&2; exit 2 ;;
  esac
}

command="${1:-}"

if [[ -z "${command}" || "${command}" == "-h" || "${command}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "WARN: ${ENV_FILE} not found; using defaults from Compose files." >&2
fi

shift || true
target="all"
if [[ $# -gt 0 ]]; then
  case "$1" in
    all|cache|lmcache|prefill|decode|gateway)
      target="$1"
      shift
      ;;
  esac
fi
extra_args=("$@")

case "${command}" in
  config)
    dc config "${extra_args[@]}"
    ;;
  up)
    read -r -a services <<<"$(services_for_target "${target}")"
    dc up -d --build "${extra_args[@]}" "${services[@]}"
    ;;
  down)
    dc down "${extra_args[@]}"
    ;;
  restart)
    read -r -a services <<<"$(services_for_target "${target}")"
    dc restart "${extra_args[@]}" "${services[@]}"
    ;;
  ps)
    dc ps "${extra_args[@]}"
    ;;
  logs)
    read -r -a services <<<"$(services_for_target "${target}")"
    dc logs -f --tail=200 "${extra_args[@]}" "${services[@]}"
    ;;
  health)
    curl -fsS "http://127.0.0.1:8000/healthz"
    echo
    ;;
  verify)
    API_URL="${API_URL:-http://127.0.0.1:8000/v1/chat/completions}" \
    GATEWAY_API_KEY="${GATEWAY_API_KEY:-sk-mvp-change-me}" \
      python "${ROOT_DIR}/backend/tests/test_verification.py"
    ;;
  doctor)
    doctor "${extra_args[@]}"
    ;;
  repair)
    doctor
    read -r -a services <<<"$(services_for_target "${target}")"
    dc up -d --build --force-recreate --remove-orphans "${extra_args[@]}" "${services[@]}"
    dc ps "${services[@]}"
    ;;
  pull)
    dc pull "${extra_args[@]}"
    ;;
  build)
    dc build gateway "${extra_args[@]}"
    ;;
  *)
    echo "Unknown command: ${command}" >&2
    usage >&2
    exit 2
    ;;
esac
