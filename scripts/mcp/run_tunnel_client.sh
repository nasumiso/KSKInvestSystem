#!/bin/sh
# キーチェーンから Runtime API Key を取得して Secure MCP Tunnel を起動する。
set -eu

KEYCHAIN_SERVICE="shintakane-tunnel-control-plane"
CONTROL_PLANE_API_KEY="$(/usr/bin/security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w)"
export CONTROL_PLANE_API_KEY

exec /opt/homebrew/bin/tunnel-client run --profile shintakane-shikiho
