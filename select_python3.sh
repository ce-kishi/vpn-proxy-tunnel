#!/usr/bin/env bash

select_python3() {
  local candidate
  local configured="${TUNNEL_PYTHON_BIN:-}"
  local candidates=()

  if [[ -n "$configured" ]]; then
    candidates=("$configured")
  else
    candidates=("$(command -v python3 || true)" /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3)
  fi

  for candidate in "${candidates[@]}"; do
    if [[ -n "$candidate" ]] && "$candidate" -c 'import socketserver' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  if [[ -n "$configured" ]]; then
    echo "TUNNEL_PYTHON_BIN cannot run Python 3 with socketserver: $configured" >&2
  else
    echo "No working Python 3 found. Check your mise Python installation or set TUNNEL_PYTHON_BIN." >&2
  fi
  return 1
}
