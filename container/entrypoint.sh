#!/bin/sh
set -eu

umask 077

die() {
    echo "entrypoint: $*" >&2
    exit 64
}

case "${AXON_INSTANCE_ID:-}" in
    ''|*[!a-z0-9-]*|-*|*-) die "invalid AXON_INSTANCE_ID" ;;
esac
[ "${#AXON_INSTANCE_ID}" -le 63 ] || die "AXON_INSTANCE_ID exceeds 63 characters"

case "${AXON_ENGINE:-}" in
    claude|codex) ;;
    *) die "AXON_ENGINE must be claude or codex" ;;
esac

[ "${AXON_CONFIG_SCHEMA:-}" = "1" ] || die "AXON_CONFIG_SCHEMA must be 1"
[ -f /etc/axon-instance/config.env ] || die "missing /etc/axon-instance/config.env"
[ -f "$PROFILE_PATH" ] || die "missing profile"
[ -d "$PROJECT_DIR" ] || die "missing workspace"
[ -d "$RELAY_DIR" ] || die "missing relay directory"
[ -d /var/log/axon ] || die "missing log directory"
[ -d "$SOCKET_DIR" ] || die "missing socket directory"

for secret_name in ANTHROPIC_API_KEY OPENAI_API_KEY SUPABASE_ANON_KEY \
    SUPABASE_SERVICE_ROLE_KEY TELEGRAM_BOT_TOKEN GROQ_API_KEY TS_AUTHKEY; do
    eval "secret_value=\${$secret_name-}"
    [ -z "$secret_value" ] || die "raw credential variable $secret_name is forbidden"
done

if [ "$AXON_ENGINE" = codex ]; then
    [ -n "${AXON_EXTENSIONS_PATH:-}" ] || die "codex requires AXON_EXTENSIONS_PATH"
    [ -f "$AXON_EXTENSIONS_PATH" ] || die "missing instance plugin"
fi

case "${AXON_OFFLINE_SMOKE:-0}" in
    1)
        export SKIP_MEMORY_FETCH=1
        export CLAUDE_PATH=/opt/axon/container/bin/fake-claude
        export CODEX_PATH=/opt/axon/container/bin/fake-codex
        ;;
    0) ;;
    *) die "AXON_OFFLINE_SMOKE must be 0 or 1" ;;
esac

if [ "$AXON_ENGINE" = claude ]; then
    command -v "$CLAUDE_PATH" >/dev/null 2>&1 || die "selected Claude CLI is unavailable"
else
    command -v "$CODEX_PATH" >/dev/null 2>&1 || die "selected Codex CLI is unavailable"
fi

conf=/run/axon/supervisord.conf
cp /opt/axon/container/supervisord.base.conf "$conf"
if [ "$AXON_ENGINE" = claude ]; then
    cat /opt/axon/container/programs/manager-claude.conf >> "$conf"
else
    cat /opt/axon/container/programs/manager-codex.conf >> "$conf"
fi

append_program() {
    enabled="$1"
    file="$2"
    case "$enabled" in
        1) cat "/opt/axon/container/programs/$file" >> "$conf" ;;
        0) ;;
        *) die "invalid enable flag for $file" ;;
    esac
}

if [ "${AXON_OFFLINE_SMOKE:-0}" = 1 ]; then
    append_program "${AXON_ENABLE_TELEGRAM:-1}" telegram-offline.conf
else
    append_program "${AXON_ENABLE_TELEGRAM:-0}" telegram.conf
fi
append_program "${AXON_ENABLE_CURATOR:-1}" curator.conf
append_program "${AXON_ENABLE_USAGE_MONITOR:-1}" usage-monitor.conf
append_program "${AXON_ENABLE_DASHBOARD:-1}" dashboard.conf
append_program "${AXON_ENABLE_WEB_CLI:-1}" web-cli-bridge.conf
append_program "${AXON_ENABLE_CLI:-1}" cli.conf

echo "entrypoint: instance=$AXON_INSTANCE_ID engine=$AXON_ENGINE offline=${AXON_OFFLINE_SMOKE:-0}"
exec supervisord -n -c "$conf"
