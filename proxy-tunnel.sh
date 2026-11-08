#!/usr/bin/env bash
# Forward the local allowlisted HTTPS proxy; run no remote commands.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/select_python3.sh"
remote_host="${TUNNEL_REMOTE_HOST:?Set TUNNEL_REMOTE_HOST (for example, user@example-host)}"
proxy_port="${TUNNEL_PROXY_PORT:-18082}"
if (($#)); then
  echo "Usage: $0" >&2
  exit 2
fi
if [[ ! "$proxy_port" =~ ^[0-9]{1,5}$ ]] || ((10#$proxy_port < 1 || 10#$proxy_port > 65535)); then
  echo "TUNNEL_PROXY_PORT must be between 1 and 65535." >&2
  exit 2
fi
proxy_port=$((10#$proxy_port))
python_bin="$(select_python3)"
proxy_pid=""
ssh_pid=""
cleanup() {
  if [[ -n "$ssh_pid" ]]; then
    kill "$ssh_pid" 2>/dev/null || true
    wait "$ssh_pid" 2>/dev/null || true
  fi
  if [[ -n "$proxy_pid" ]]; then
    kill "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

"$python_bin" "$script_dir/http_connect_proxy.py" "$proxy_port" &
proxy_pid=$!
sleep 0.3
if ! kill -0 "$proxy_pid" 2>/dev/null; then
  echo "Proxy failed to start on 127.0.0.1:$proxy_port." >&2
  exit 1
fi
echo "Starting SSH tunnel to $remote_host (remote proxy: http://127.0.0.1:$proxy_port)."
echo "Keep this terminal open. Press Ctrl+C to stop the tunnel and proxy."
ssh -N -T -o ExitOnForwardFailure=yes -o ConnectTimeout=15 \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -R "127.0.0.1:$proxy_port:127.0.0.1:$proxy_port" \
  "$remote_host" &
ssh_pid=$!
wait "$ssh_pid"
