#!/usr/bin/env bash

opencode_backend_unhealthy_log() {
  file="$1"
  [ -f "$file" ] || return 1

  if grep -Eiq 'HTTP Error 5[0-9][0-9]|HTTP[^[:alnum:]]+5[0-9][0-9]|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout' "$file"; then
    return 0
  fi

  if grep -Eiq 'ConnectionRefusedError|Connection refused|Connection closed|ECONNREFUSED|ECONNRESET|EHOSTUNREACH|ENETUNREACH|Network is unreachable|Remote end closed connection|urlopen error.*(timed out|refused|reset|unreachable)|TimeoutError|timed out waiting for assistant reply|did not become healthy' "$file"; then
    return 0
  fi

  return 1
}

opencode_backend_skip() {
  echo "SKIP  $1 (OpenCode/LiteLLM backend unhealthy)"
  exit 0
}
