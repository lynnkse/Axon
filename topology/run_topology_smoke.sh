#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

runtime=${AXON_RUNTIME:-runsc}
fixture_image=axon-network-fixture:step6
proxy_image=axon-research-proxy:step5
firewall_image=axon-firewall-helper:step6
prefix=axon-s6

containers="a-engine a-proxy a-broker a-gate b-engine b-proxy b-broker b-gate"
networks="a-private a-broker a-research-up a-service-up b-private b-broker b-research-up b-service-up"

cleanup() {
    fw delete AX6AR 172.31.12.20 fd00:6:12::20 >/dev/null 2>&1 || true
    fw delete AX6AS 172.31.13.20 fd00:6:13::20 >/dev/null 2>&1 || true
    fw delete AX6BR 172.31.22.20 fd00:6:22::20 >/dev/null 2>&1 || true
    fw delete AX6BS 172.31.23.20 fd00:6:23::20 >/dev/null 2>&1 || true
    for role in $containers; do docker rm -f "$prefix-$role" >/dev/null 2>&1 || true; done
    for item in $networks; do docker network rm "$prefix-$item" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT INT TERM

fw() {
    docker run --rm --runtime=runc --network=host --cap-drop=ALL --cap-add=NET_ADMIN --cap-add=NET_RAW "$firewall_image" "$@"
}

docker build -f topology/Dockerfile.firewall -t "$firewall_image" .
cleanup

docker build -f topology/Dockerfile.fixture -t "$fixture_image" .
docker build -f sandbox/Dockerfile.proxy -t "$proxy_image" .

make_net() {
    name=$1; v4=$2; v6=$3; mode=$4
    if [ "$mode" = internal ]; then
        docker network create --driver bridge --internal --ipv6 --subnet "$v4" --subnet "$v6" "$prefix-$name" >/dev/null
    else
        docker network create --driver bridge --ipv6 --subnet "$v4" --subnet "$v6" "$prefix-$name" >/dev/null
    fi
}

make_net a-private 172.31.10.0/24 fd00:6:10::/64 internal
make_net a-broker 172.31.11.0/24 fd00:6:11::/64 internal
make_net a-research-up 172.31.12.0/24 fd00:6:12::/64 egress
make_net a-service-up 172.31.13.0/24 fd00:6:13::/64 egress
make_net b-private 172.31.20.0/24 fd00:6:20::/64 internal
make_net b-broker 172.31.21.0/24 fd00:6:21::/64 internal
make_net b-research-up 172.31.22.0/24 fd00:6:22::/64 egress
make_net b-service-up 172.31.23.0/24 fd00:6:23::/64 egress

create_fixture() {
    name=$1; net=$2; v4=$3; v6=$4; shift 4
    docker create --name "$prefix-$name" --runtime="$runtime" --network "$prefix-$net" --ip "$v4" --ip6 "$v6" --read-only --cap-drop=ALL --security-opt=no-new-privileges:true --pids-limit=64 --memory=128m --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m "$fixture_image" "$@" >/dev/null
}

create_fixture a-engine a-private 172.31.10.10 fd00:6:10::10 serve --ports 9000 --unix /tmp/manager.sock
create_fixture a-broker a-private 172.31.10.30 fd00:6:10::30 serve --ports 8443,8444,8445,8446
docker network connect --ip 172.31.11.10 --ip6 fd00:6:11::10 "$prefix-a-broker" "$prefix-a-broker"
create_fixture a-gate a-broker 172.31.11.20 fd00:6:11::20 serve --ports 9443
docker network connect --ip 172.31.13.20 --ip6 fd00:6:13::20 "$prefix-a-service-up" "$prefix-a-gate"

create_fixture b-engine b-private 172.31.20.10 fd00:6:20::10 serve --ports 9000 --unix /tmp/manager.sock
create_fixture b-broker b-private 172.31.20.30 fd00:6:20::30 serve --ports 8443,8444,8445,8446
docker network connect --ip 172.31.21.10 --ip6 fd00:6:21::10 "$prefix-b-broker" "$prefix-b-broker"
create_fixture b-gate b-broker 172.31.21.20 fd00:6:21::20 serve --ports 9443
docker network connect --ip 172.31.23.20 --ip6 fd00:6:23::20 "$prefix-b-service-up" "$prefix-b-gate"

create_proxy() {
    id=$1; net=$2; up=$3; v4=$4; v6=$5; up4=$6; up6=$7
    docker create --name "$prefix-$id-proxy" --runtime="$runtime" --network "$prefix-$net" --ip "$v4" --ip6 "$v6" --read-only --cap-drop=ALL --security-opt=no-new-privileges:true --pids-limit=64 --memory=256m --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m -v "$PWD/sandbox/example.policy.json:/etc/axon-instance/sandbox-policy.json:ro" "$proxy_image" >/dev/null
    docker network connect --ip "$up4" --ip6 "$up6" "$prefix-$up" "$prefix-$id-proxy"
}
create_proxy a a-private a-research-up 172.31.10.20 fd00:6:10::20 172.31.12.20 fd00:6:12::20
create_proxy b b-private b-research-up 172.31.20.20 fd00:6:20::20 172.31.22.20 fd00:6:22::20

for role in $containers; do docker start "$prefix-$role" >/dev/null; done
sleep 2

fw add AX6AR 172.31.12.20 fd00:6:12::20
fw add AX6AS 172.31.13.20 fd00:6:13::20
fw add AX6BR 172.31.22.20 fd00:6:22::20
fw add AX6BS 172.31.23.20 fd00:6:23::20

echo "=== runtime and attachment proof ==="
for role in $containers; do
    docker inspect --format '{{.Name}} runtime={{.HostConfig.Runtime}} readonly={{.HostConfig.ReadonlyRootfs}} networks={{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$prefix-$role"
done

echo "=== intended paths ==="
docker exec "$prefix-a-engine" python /opt/axon-net/net_fixture.py fetch-probe 172.31.10.20
for port in 8443 8444 8445 8446; do docker exec "$prefix-a-engine" python /opt/axon-net/net_fixture.py probe 172.31.10.30 "$port" --expect allow; done
docker exec "$prefix-a-broker" python /opt/axon-net/net_fixture.py probe 172.31.11.20 9443 --expect allow
docker exec "$prefix-b-broker" python /opt/axon-net/net_fixture.py probe fd00:6:21::20 9443 --expect allow

echo "=== cross-instance IPv4 denials ==="
for target in 172.31.20.20:8787 172.31.20.30:8443 172.31.20.10:9000; do
    host=${target%:*}; port=${target#*:}
    docker exec "$prefix-a-engine" python /opt/axon-net/net_fixture.py probe "$host" "$port" --expect deny
done
echo "=== cross-instance IPv6 denials ==="
docker exec "$prefix-a-engine" python /opt/axon-net/net_fixture.py probe fd00:6:20::20 8787 --expect deny
docker exec "$prefix-a-engine" python /opt/axon-net/net_fixture.py probe fd00:6:20::30 8443 --expect deny
docker exec "$prefix-a-engine" python /opt/axon-net/net_fixture.py probe fd00:6:20::10 9000 --expect deny

echo "=== role and direct-egress denials ==="
docker exec "$prefix-a-engine" python /opt/axon-net/net_fixture.py probe 172.31.11.20 9443 --expect deny
docker exec "$prefix-a-engine" python /opt/axon-net/net_fixture.py probe 1.1.1.1 443 --expect deny
docker exec "$prefix-a-engine" python /opt/axon-net/net_fixture.py probe 2606:4700:4700::1111 443 --expect deny
docker exec "$prefix-a-proxy" python -c 'import socket; socket.create_connection(("172.31.13.20",9443),1)' >/dev/null 2>&1 && exit 1 || echo research_to_service_gateway=denied
docker exec "$prefix-a-gate" python /opt/axon-net/net_fixture.py probe 172.31.10.20 8787 --expect deny
docker exec "$prefix-a-proxy" python -c 'import socket; socket.create_connection(("1.1.1.1",80),1)' >/dev/null 2>&1 && exit 1 || echo research_raw_tcp80=denied
docker exec "$prefix-a-gate" python -c 'import socket; socket.create_connection(("1.1.1.1",80),1)' >/dev/null 2>&1 && exit 1 || echo service_raw_tcp80=denied
fw list AX6AR 172.31.12.20 fd00:6:12::20
fw list AX6AS 172.31.13.20 fd00:6:13::20

echo "=== socket namespace denial ==="
docker exec "$prefix-a-engine" test -S /tmp/manager.sock
docker exec "$prefix-b-engine" test -S /tmp/manager.sock
docker exec "$prefix-a-engine" test ! -e /tmp/instance-b/manager.sock
echo unix_sockets=per-container-namespaces

echo "STEP6_TOPOLOGY_SMOKE=PASS"
