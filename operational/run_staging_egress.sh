#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

prefix=axon-s7
runtime=${AXON_RUNTIME:-runsc}
fixture_image=axon-network-fixture:step7
broker_image=axon-service-broker:step7
gate_image=axon-service-gateway:step7
firewall_image=axon-firewall-helper:step6

fw() {
    docker run --rm --runtime=runc --network=host --cap-drop=ALL --cap-add=NET_ADMIN --cap-add=NET_RAW "$firewall_image" "$@"
}
cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        docker exec "$prefix-gate" cat /etc/resolv.conf 2>/dev/null || true
        docker exec "$prefix-gate" cat /proc/net/route 2>/dev/null || true
        docker logs "$prefix-gate" 2>/dev/null || true
        fw list AX7SG 172.31.32.20 fd00:7:32::20 2>/dev/null || true
    fi
    fw delete AX7SG 172.31.32.20 fd00:7:32::20 >/dev/null 2>&1 || true
    for name in engine broker gate; do docker rm -f "$prefix-$name" >/dev/null 2>&1 || true; done
    for name in private broker-link service-up; do docker network rm "$prefix-$name" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT INT TERM

docker build -f topology/Dockerfile.firewall -t "$firewall_image" .
cleanup
docker build -f topology/Dockerfile.fixture -t "$fixture_image" .
docker build -f operational/Dockerfile.broker -t "$broker_image" .
docker build -f operational/Dockerfile.gateway -t "$gate_image" .

docker network create --driver bridge --internal --ipv6 --subnet 172.31.30.0/24 --subnet fd00:7:30::/64 "$prefix-private" >/dev/null
docker network create --driver bridge --internal --ipv6 --subnet 172.31.31.0/24 --subnet fd00:7:31::/64 "$prefix-broker-link" >/dev/null
docker network create --driver bridge --ipv6 --subnet 172.31.32.0/24 --subnet fd00:7:32::/64 "$prefix-service-up" >/dev/null

docker create --name "$prefix-engine" --runtime="$runtime" --network "$prefix-private" --ip 172.31.30.10 --ip6 fd00:7:30::10 --read-only --cap-drop=ALL --security-opt=no-new-privileges:true --pids-limit=64 --memory=128m --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m "$fixture_image" serve --ports 9000 >/dev/null
docker create --name "$prefix-broker" --runtime="$runtime" --network "$prefix-private" --ip 172.31.30.30 --ip6 fd00:7:30::30 --read-only --cap-drop=ALL --security-opt=no-new-privileges:true --pids-limit=64 --memory=128m -e AXON_SERVICE_GATE=172.31.31.20 "$broker_image" >/dev/null
docker network connect --ip 172.31.31.10 --ip6 fd00:7:31::10 "$prefix-broker-link" "$prefix-broker"
docker create --name "$prefix-gate" --runtime="$runtime" --network "$prefix-broker-link" --ip 172.31.31.20 --ip6 fd00:7:31::20 --dns 192.168.1.1 --read-only --cap-drop=ALL --security-opt=no-new-privileges:true --pids-limit=64 --memory=192m -v "$PWD/operational/manifest.json:/etc/axon-service/manifest.json:ro" "$gate_image" >/dev/null
docker network connect --gw-priority 1 --ip 172.31.32.20 --ip6 fd00:7:32::20 "$prefix-service-up" "$prefix-gate"

for name in engine broker gate; do docker start "$prefix-$name" >/dev/null; done
fw add AX7SG 172.31.32.20 fd00:7:32::20
sleep 2

echo "=== runtime and paths ==="
for name in engine broker gate; do docker inspect --format '{{.Name}} runtime={{.HostConfig.Runtime}} readonly={{.HostConfig.ReadonlyRootfs}} networks={{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$prefix-$name"; done

echo "=== real broker-to-gateway HTTPS reachability ==="
docker exec "$prefix-engine" python /opt/axon-net/net_fixture.py json-probe 172.31.30.30 8443 claude
docker exec "$prefix-engine" python /opt/axon-net/net_fixture.py json-probe 172.31.30.30 8448 codex
docker exec "$prefix-engine" python /opt/axon-net/net_fixture.py json-probe 172.31.30.30 8444 supabase
docker exec "$prefix-engine" python /opt/axon-net/net_fixture.py json-probe 172.31.30.30 8445 telegram
docker exec "$prefix-engine" python /opt/axon-net/net_fixture.py json-probe 172.31.30.30 8446 groq

echo "=== visible fail-closed denials ==="
docker exec "$prefix-broker" python -c 'import socket,json; s=socket.create_connection(("172.31.31.20",9443),3); s.sendall(b"{\"service\":\"unknown\"}\n"); print(s.makefile("rb").readline().decode().strip())'
docker exec "$prefix-broker" python -c 'import socket,json; s=socket.create_connection(("172.31.31.20",9443),3); s.sendall(b"{\"service\":\"claude\",\"host\":\"example.com\"}\n"); print(s.makefile("rb").readline().decode().strip())'
docker exec "$prefix-engine" python /opt/axon-net/net_fixture.py probe 172.31.31.20 9443 --expect deny
docker exec "$prefix-gate" python -c 'import socket; socket.create_connection(("1.1.1.1",80),2)' >/dev/null 2>&1 && exit 1 || echo non_allowlisted_port=network_denied
fw list AX7SG 172.31.32.20 fd00:7:32::20

echo "=== safe gate audit ==="
docker logs "$prefix-gate"
echo "STEP7_STAGING_EGRESS=PASS"
