#!/bin/sh
# Install/remove a dedicated source-address egress chain in DOCKER-USER.
set -eu
iptables() { command iptables-legacy "$@"; }
ip6tables() { command ip6tables-legacy "$@"; }
action=$1
chain=$2
v4=$3
v6=$4
dns4=${AXON_CONTROLLED_DNS_V4:-192.168.1.1}

if [ "$action" = list ]; then
    iptables -L "$chain" -n -v
    ip6tables -L "$chain" -n -v
    exit 0
fi

if [ "$action" = delete ]; then
    iptables -D DOCKER-USER -s "$v4" -j "$chain" 2>/dev/null || true
    iptables -F "$chain" 2>/dev/null || true
    iptables -X "$chain" 2>/dev/null || true
    ip6tables -D DOCKER-USER -s "$v6" -j "$chain" 2>/dev/null || true
    ip6tables -F "$chain" 2>/dev/null || true
    ip6tables -X "$chain" 2>/dev/null || true
    exit 0
fi

# Public HTTPS and DNS only. RFC/private/special ranges are rejected first.
iptables -N "$chain"
iptables -A "$chain" -d "$dns4" -p udp --dport 53 -j ACCEPT
iptables -A "$chain" -d "$dns4" -p tcp --dport 53 -j ACCEPT
for cidr in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 224.0.0.0/4 240.0.0.0/4; do
    iptables -A "$chain" -d "$cidr" -j REJECT
done
iptables -A "$chain" -p tcp --dport 443 -j ACCEPT
iptables -A "$chain" -j REJECT
iptables -I DOCKER-USER 1 -s "$v4" -j "$chain"

ip6tables -N "$chain"
for cidr in ::/128 ::1/128 64:ff9b:1::/48 100::/64 2001:db8::/32 2001:10::/28 fc00::/7 fe80::/10 ff00::/8; do
    ip6tables -A "$chain" -d "$cidr" -j REJECT
done
ip6tables -A "$chain" -p udp --dport 53 -j ACCEPT
ip6tables -A "$chain" -p tcp --dport 53 -j ACCEPT
ip6tables -A "$chain" -p tcp --dport 443 -j ACCEPT
ip6tables -A "$chain" -j REJECT
ip6tables -I DOCKER-USER 1 -s "$v6" -j "$chain"
