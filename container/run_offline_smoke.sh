#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
image="${AXON_SMOKE_IMAGE:-axon-engine:step4}"
runtime="${AXON_RUNTIME:-runsc}"
engine="${AXON_ENGINE:-claude}"
name="axon-step4-$engine"

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker build -t "$image" .
cleanup

docker volume create "${name}-workspace" >/dev/null
docker volume create "${name}-state" >/dev/null
docker volume create "${name}-logs" >/dev/null

docker run -d --name "$name" \
    --runtime="$runtime" \
    --network=none \
    --read-only \
    --cap-drop=ALL \
    --security-opt=no-new-privileges:true \
    --pids-limit=512 \
    --memory=6g --memory-swap=6g --cpus=4 \
    --tmpfs /run/axon:rw,nosuid,nodev,noexec,size=32m,uid=10001,gid=10001,mode=0700 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=512m,uid=10001,gid=10001,mode=0700 \
    -v "${name}-workspace:/workspace" \
    -v "${name}-state:/var/lib/axon" \
    -v "${name}-logs:/var/log/axon" \
    -v "$PWD/container/smoke/config.env:/etc/axon-instance/config.env:ro" \
    -v "$PWD/container/smoke/profile.md:/etc/axon-instance/profile.md:ro" \
    -v "$PWD/container/smoke:/etc/axon-instance/plugin:ro" \
    -e AXON_INSTANCE_ID=step4-smoke \
    -e AXON_INSTANCE=step4-smoke \
    -e AXON_CONFIG_SCHEMA=1 \
    -e AXON_ENGINE="$engine" \
    -e AXON_OFFLINE_SMOKE=1 \
    -e AXON_EXTENSIONS_PATH=/etc/axon-instance/plugin/axon_ext.py \
    -e AXON_ENABLE_TELEGRAM=1 \
    -e AXON_ENABLE_CURATOR=1 \
    -e AXON_ENABLE_USAGE_MONITOR=1 \
    -e AXON_ENABLE_DASHBOARD=1 \
    -e AXON_ENABLE_WEB_CLI=1 \
    -e AXON_ENABLE_CLI=1 \
    "$image" >/dev/null

for ignored in $(seq 1 60); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$name")
    [ "$status" = healthy ] && break
    [ "$status" = unhealthy ] && { docker logs "$name"; exit 1; }
    sleep 1
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" = healthy ]

echo "=== initial supervisor state ==="
docker inspect -f 'runtime={{.HostConfig.Runtime}} network={{.HostConfig.NetworkMode}} readonly={{.HostConfig.ReadonlyRootfs}}' "$name"
docker exec "$name" supervisorctl -c /run/axon/supervisord.conf status
echo "=== socket and namespace proof ==="
docker exec "$name" python /opt/axon/container/healthcheck.py
docker exec "$name" sh -c 'test ! -e /sys/class/net/eth0 && echo network=loopback-only; stat -c "%a %u:%g %n" /run/axon/*.sock'

docker exec "$name" sh -c 'test ! -w /opt/axon && printf state > /var/lib/axon/persistence-marker && printf workspace > /workspace/persistence-marker && printf logs > /var/log/axon/persistence-marker'
old_pid=$(docker exec "$name" supervisorctl -c /run/axon/supervisord.conf pid manager)
docker exec "$name" supervisorctl -c /run/axon/supervisord.conf signal KILL manager >/dev/null
for ignored in $(seq 1 30); do
    new_pid=$(docker exec "$name" supervisorctl -c /run/axon/supervisord.conf pid manager)
    manager_state=$(docker exec "$name" supervisorctl -c /run/axon/supervisord.conf status manager | awk '{print $2}')
    [ "$new_pid" != 0 ] && [ "$new_pid" != "$old_pid" ] && [ "$manager_state" = RUNNING ] && break
    sleep 1
done
[ "$new_pid" != 0 ]
[ "$new_pid" != "$old_pid" ]
[ "$manager_state" = RUNNING ]
echo "manager_recovery=$old_pid->$new_pid"
docker exec "$name" python /opt/axon/container/healthcheck.py

docker stop -t 35 "$name" >/dev/null
echo "shutdown_exit=$(docker inspect -f '{{.State.ExitCode}}' "$name")"
docker start "$name" >/dev/null
for ignored in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" = healthy ] && break
    sleep 1
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" = healthy ]
docker exec "$name" sh -c 'test "$(cat /var/lib/axon/persistence-marker)" = state && test "$(cat /workspace/persistence-marker)" = workspace && test "$(cat /var/log/axon/persistence-marker)" = logs'
echo "persistence=state,workspace,logs-retained"
docker exec "$name" python /opt/axon/container/healthcheck.py
