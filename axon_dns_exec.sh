#!/bin/bash
# Temporary process-local TCP DNS; no host resolver or network namespace change.
set -euo pipefail
AXON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/bwrap --unshare-user --uid "$(id -u)" --gid "$(id -g)" \
  --bind / / --dev-bind /dev /dev --ro-bind "$AXON_DIR/axon_dns_resolv.conf" /etc/resolv.conf \
  --ro-bind "$AXON_DIR/axon_dns_hosts" /etc/hosts \
  --cap-drop ALL -- "$@"
