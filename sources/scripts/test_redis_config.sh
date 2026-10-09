#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "Redis configuration test failed: $*" >&2
  exit 1
}

command -v jq >/dev/null 2>&1 || fail "jq is not available in the DocumentServer image"
[[ -x /usr/local/bin/docker-entrypoint.sh ]] || fail "the native DocumentServer entrypoint is not available"

[[ -n "${REDIS_SERVER_DB_NUM:-}" ]] || fail "REDIS_SERVER_DB_NUM is not set"

if [[ -n "${REDIS_SENTINEL_NODES:-}" && -n "${REDIS_CLUSTER_NODES:-}" ]]; then
  fail "Redis Sentinel and Cluster nodes cannot both be set"
elif [[ -n "${REDIS_SENTINEL_NODES:-}" ]]; then
  topology=sentinel
  [[ -n "${REDIS_SENTINEL_GROUP_NAME:-}" ]] || fail "REDIS_SENTINEL_GROUP_NAME is not set"
elif [[ -n "${REDIS_CLUSTER_NODES:-}" ]]; then
  topology=cluster
else
  fail "Redis Sentinel or Cluster nodes must be set"
fi

# The orchestrated entrypoint is the component that turns Helm's environment
# into NODE_CONFIG. ENTRYPOINT_CONFIG_ONLY prevents DocService from starting;
# the regular Helm test pod separately checks that DocService starts and
# connects successfully.
render_config() {
  # Explicitly remove the obsolete aliases while exercising the native names.
  env -u REDIS_SERVER_PASS -u REDIS_SENTINEL_PASS -u REDIS_SERVER_DB \
    "$@" /usr/local/bin/docker-entrypoint.sh docservice
}

check_config() {
  local name="$1"
  local node_config="$2"
  local node_auth="$3"
  local sentinel_auth="$4"

  printf '%s' "$node_config" | jq -e '
  .services.CoAuthoring.server.editorDataStorage == "editorDataRedis" and
  .services.CoAuthoring.server.editorStatStorage == "editorDataRedis" and
  .services.CoAuthoring.redis.name == "redis"
' >/dev/null || fail "$name: NODE_CONFIG does not select the native Redis backend"

  if [[ "$topology" == sentinel ]]; then
    printf '%s' "$node_config" | jq -e --arg nodes "$REDIS_SENTINEL_NODES" --arg db "$REDIS_SERVER_DB_NUM" '
    (.services.CoAuthoring.redis.optionsSentinel | type) == "object" and
    (.services.CoAuthoring.redis.optionsSentinel.sentinelRootNodes |
      map("\(.host):\(.port)") == ($nodes | split(" "))) and
    (.services.CoAuthoring.redis.optionsSentinel.nodeClientOptions.database == ($db | tonumber)) and
    ((.services.CoAuthoring.redis.optionsCluster.rootNodes // []) | length) == 0
    ' >/dev/null || fail "$name: NODE_CONFIG does not contain the expected native Sentinel configuration"

    if [[ "$node_auth" == true ]]; then
      printf '%s' "$node_config" | jq -e '
      .services.CoAuthoring.redis.optionsSentinel.nodeClientOptions |
      has("username") and has("password") and has("database")
      ' >/dev/null || fail "$name: authenticated Redis node options are incomplete"
    else
      printf '%s' "$node_config" | jq -e '
      .services.CoAuthoring.redis.optionsSentinel.nodeClientOptions |
      (has("username") or has("password")) | not
      ' >/dev/null || fail "$name: unauthenticated Redis node options contain credentials"
    fi

    if [[ "$sentinel_auth" == true ]]; then
      printf '%s' "$node_config" | jq -e '
      .services.CoAuthoring.redis.optionsSentinel.sentinelClientOptions |
      has("username") and has("password")
      ' >/dev/null || fail "$name: authenticated Sentinel options are incomplete"
    else
      printf '%s' "$node_config" | jq -e '
      .services.CoAuthoring.redis.optionsSentinel.sentinelClientOptions == {}
      ' >/dev/null || fail "$name: unauthenticated Sentinel options are not empty"
    fi
  else
    printf '%s' "$node_config" | jq -e --arg nodes "$REDIS_CLUSTER_NODES" '
    (.services.CoAuthoring.redis.optionsCluster.rootNodes |
      map(.url) == ($nodes | split(" ") | map("redis://" + .))) and
    ((.services.CoAuthoring.redis.optionsSentinel // {}) | length) == 0
    ' >/dev/null || fail "$name: NODE_CONFIG does not contain the expected native Cluster configuration"

    if [[ "$node_auth" == true ]]; then
      printf '%s' "$node_config" | jq -e '
      .services.CoAuthoring.redis.optionsCluster.defaults |
      has("username") and has("password")
      ' >/dev/null || fail "$name: authenticated Cluster options are incomplete"
    else
      printf '%s' "$node_config" | jq -e '
      .services.CoAuthoring.redis.optionsCluster.defaults |
      (has("username") or has("password")) | not
      ' >/dev/null || fail "$name: unauthenticated Cluster options contain credentials"
    fi
  fi
}

node_config=$(render_config)
check_config "deployed environment" "$node_config" \
  "$([[ -n "${REDIS_SERVER_PWD:-}" ]] && echo true || echo false)" \
  "$([[ -n "${REDIS_SENTINEL_PWD:-}" ]] && echo true || echo false)"

# Exercise the supported authentication matrix using the native environment
# names. These checks only render NODE_CONFIG; the regular test-ds hook checks
# the actual Redis/Sentinel and DocService connections.
if [[ "$topology" == sentinel ]]; then
  check_config "unauthenticated Redis and Sentinel" \
    "$(render_config REDIS_SERVER_USER= REDIS_SERVER_PWD= REDIS_SENTINEL_USER= REDIS_SENTINEL_PWD=)" false false
  check_config "authenticated Redis and unauthenticated Sentinel" \
    "$(render_config REDIS_SERVER_USER=default REDIS_SERVER_PWD=redis-test REDIS_SENTINEL_USER= REDIS_SENTINEL_PWD=)" true false
  check_config "authenticated Redis and Sentinel" \
    "$(render_config REDIS_SERVER_USER=default REDIS_SERVER_PWD=redis-test REDIS_SENTINEL_USER=sentinel-test REDIS_SENTINEL_PWD=sentinel-test)" true true
else
  check_config "unauthenticated Cluster" \
    "$(render_config REDIS_SERVER_USER= REDIS_SERVER_PWD=)" false false
  check_config "authenticated Cluster" \
    "$(render_config REDIS_SERVER_USER=default REDIS_SERVER_PWD=redis-test)" true false
fi

echo "Redis native $topology configuration is valid"
