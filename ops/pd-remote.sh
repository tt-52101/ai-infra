#!/usr/bin/env bash
set -euo pipefail

remote="${PD_REMOTE:-${1:-}}"
if [[ -n "${1:-}" ]]; then
  shift
fi

remote_dir="${PD_REMOTE_DIR:-/data/temp/txs/ai-infra}"
remote_port="${PD_REMOTE_PORT:-22}"
command="${1:-doctor}"
target="${2:-}"

usage() {
  cat <<'USAGE'
Usage:
  PD_REMOTE=root@117.190.94.226 [PD_REMOTE_PORT=24132] [PD_REMOTE_DIR=/data/temp/txs/ai-infra] ops/pd-remote.sh <command> [target]
  ops/pd-remote.sh root@node-2 <command> [target]

Commands:
  doctor          Run remote docker/Compose config checks
  repair          Force recreate remote target service group
  logs            Tail remote target logs
  ps              Show remote service status
  health          Check remote gateway health endpoint
  verify          Run remote MVP TTFT verification
  exec            Run a remote command in the repo directory
  shell           Open an interactive SSH shell in remote repo directory

Examples:
  PD_REMOTE=root@117.190.94.226 PD_REMOTE_PORT=24132 bash ops/pd-remote.sh doctor
  PD_REMOTE=root@117.190.94.226 PD_REMOTE_PORT=24132 bash ops/pd-remote.sh repair prefill
  PD_REMOTE=root@117.190.94.226 PD_REMOTE_PORT=24132 bash ops/pd-remote.sh logs prefill

Password auth:
  Set PD_REMOTE_PASSWORD in the caller environment. If sshpass is installed,
  it is used via SSHPASS. Otherwise OpenSSH askpass is used when available.
USAGE
}

if [[ -z "${remote}" || "${remote}" == "-h" || "${remote}" == "--help" ]]; then
  usage
  exit 2
fi

ssh_opts=(
  -p "${remote_port}"
  -o ConnectTimeout="${PD_REMOTE_CONNECT_TIMEOUT:-10}"
  -o ServerAliveInterval="${PD_REMOTE_SERVER_ALIVE_INTERVAL:-30}"
)

askpass_file=""
cleanup() {
  if [[ -n "${askpass_file}" && -f "${askpass_file}" ]]; then
    rm -f "${askpass_file}"
  fi
}
trap cleanup EXIT

ssh_command=(ssh)
if [[ -n "${PD_REMOTE_PASSWORD:-}" ]]; then
  if command -v sshpass >/dev/null 2>&1; then
    export SSHPASS="${PD_REMOTE_PASSWORD}"
    ssh_command=(sshpass -e ssh)
    ssh_opts+=(-o BatchMode=no)
  else
    askpass_file="$(mktemp)"
    cat >"${askpass_file}" <<'ASKPASS'
#!/usr/bin/env bash
printf '%s\n' "${PD_REMOTE_PASSWORD}"
ASKPASS
    chmod 700 "${askpass_file}"
    export SSH_ASKPASS="${askpass_file}"
    export SSH_ASKPASS_REQUIRE=force
    export DISPLAY="${DISPLAY:-pd-remote}"
    ssh_opts+=(-o BatchMode=no)
  fi
else
  ssh_opts+=(-o BatchMode=yes)
fi

remote_stack() {
  local stack_command="$1"
  local stack_target="${2:-}"
  # Remote examples: bash ops/pd-stack.sh doctor; bash ops/pd-stack.sh repair prefill; bash ops/pd-stack.sh logs prefill.
  local stack_line="cd '${remote_dir}' && docker compose version && bash ops/pd-stack.sh ${stack_command}"
  if [[ -n "${stack_target}" ]]; then
    stack_line+=" ${stack_target}"
  fi
  "${ssh_command[@]}" "${ssh_opts[@]}" "${remote}" "${stack_line}"
}

case "${command}" in
  doctor)
    remote_stack doctor
    ;;
  repair)
    remote_stack repair "${target:-prefill}"
    ;;
  logs)
    remote_stack logs "${target:-prefill}"
    ;;
  ps)
    remote_stack ps "${target}"
    ;;
  health)
    remote_stack health
    ;;
  verify)
    remote_stack verify
    ;;
  exec)
    if [[ -z "${target}" ]]; then
      echo "exec requires a remote command argument" >&2
      exit 2
    fi
    "${ssh_command[@]}" "${ssh_opts[@]}" "${remote}" "cd '${remote_dir}' && ${target}"
    ;;
  shell)
    "${ssh_command[@]}" "${ssh_opts[@]}" -t "${remote}" "cd '${remote_dir}' && exec bash"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: ${command}" >&2
    usage >&2
    exit 2
    ;;
esac
