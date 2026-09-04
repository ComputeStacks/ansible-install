#!/usr/bin/env bash
#
# ComputeStacks controller management CLI (provisioner v2).
#
# Installed at /usr/local/bin/cstacks by roles/controller. Every setting comes
# from the environment file below, which ansible owns; this script contains no
# environment-specific values so it can be dropped, unchanged, onto a
# v1-provisioned controller in attach mode (roles/controller/tasks/
# attach_prep.yml). That is why every value read from the environment file has
# a fallback: on an existing controller that file is APPEND-ONLY and carries
# only what v1 wrote plus the two keys attach mode adds.
#
# `cstacks runner '<ruby>'` is a cross-role contract (docs/contracts.md):
# stdout is passed through untouched and the exit code is propagated.

set -euo pipefail

CS_ENV_FILE="${CS_ENV_FILE:-/etc/default/computestacks}"

if [ ! -r "$CS_ENV_FILE" ]; then
  echo "cstacks: cannot read $CS_ENV_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090,SC1091
. "$CS_ENV_FILE"

# --- defaults for everything the environment file may not carry ------------
: "${CS_APP_ID:=default}"
: "${CS_CERT_PATH:=/etc/computestacks/certificates}"
: "${CS_SSH_KEYS_PATH:=/etc/computestacks/.ssh}"
: "${CS_BRANDING_PATH:=/var/lib/computestacks/branding}"
: "${CS_PROXY_IPS_PATH:=/var/lib/computestacks/proxy_ips}"
: "${CS_STATE_PATH:=/var/lib/computestacks}"
: "${DB_BACKUPS_PATH:=/var/lib/computestacks/backups}"
: "${CS_LOCALE:=en}"
: "${CS_CURRENCY:=USD}"
: "${CS_DB_NAME:=cloudportal}"
: "${CS_CONTAINER_NAME:=portal}"
: "${REDIS_HOST:=127.0.0.1}"
: "${REDIS_PORT:=6379}"
: "${RAILS_MAX_THREADS:=15}"
: "${RAILS_MIN_THREADS:=15}"
: "${WEB_CONCURRENCY:=2}"
: "${QUEUE_SYSTEM:=15}"
: "${QUEUE_DEPLOYMENTS:=20}"
: "${QUEUE_LE:=2}"
: "${SENTRY_DSN:=}"
: "${NODE_ENROLLMENT_TOKEN:=}"
: "${MALLOC_CONF:=dirty_decay_ms:1000,narenas:2,background_thread:true,stats_print:false}"
: "${RUBY_YJIT_ENABLE:=1}"

# CS_REG is what v1 wrote and what every existing controller has. v2 also
# writes CS_IMAGE_REPO/CS_IMAGE_TAG so an operator can see the two halves.
if [ -z "${CS_REG:-}" ]; then
  if [ -n "${CS_IMAGE_REPO:-}" ] && [ -n "${CS_IMAGE_TAG:-}" ]; then
    CS_REG="${CS_IMAGE_REPO}:${CS_IMAGE_TAG}"
  else
    echo "cstacks: neither CS_REG nor CS_IMAGE_REPO/CS_IMAGE_TAG is set in $CS_ENV_FILE" >&2
    exit 1
  fi
fi

for required in SECRET_KEY_BASE USER_AUTH_SECRET DATABASE_URL; do
  if [ -z "${!required:-}" ]; then
    echo "cstacks: $required is empty in $CS_ENV_FILE" >&2
    exit 1
  fi
done

CONTAINER="$CS_CONTAINER_NAME"
DB_SENTINEL="${CS_STATE_PATH}/.db_provisioned"

# --- shared container arguments -------------------------------------------
# Built into arrays so every subcommand launches the portal image the same
# way. v1 repeated the whole flag list in seven places and they had drifted.

ENV_ARGS=()
MOUNT_ARGS=()
# Appended verbatim to every container launch. Subcommands set it before
# calling one_off/one_off_tty and reset it afterwards.
EXTRA_ARGS=()

# `-it` only when there really is a terminal: `cstacks test` is also run
# non-interactively (by the validate role and by hand over ssh -T), and
# `docker run -it` without a tty aborts.
tty_args()
{
  TTY_ARGS=()
  if [ -t 0 ] && [ -t 1 ]; then
    TTY_ARGS=(-it)
  fi
}

term_size_args()
{
  TERM_ARGS=(
    -e "COLUMNS=$(tput cols 2>/dev/null || echo 80)"
    -e "LINES=$(tput lines 2>/dev/null || echo 24)"
  )
}

build_env_args()
{
  ENV_ARGS=(
    -e "APP_ID=$CS_APP_ID"
    -e "CURRENCY=$CS_CURRENCY"
    -e "LOCALE=$CS_LOCALE"
    -e "SECRET_KEY_BASE=$SECRET_KEY_BASE"
    -e "USER_AUTH_SECRET=$USER_AUTH_SECRET"
    -e "NODE_ENROLLMENT_TOKEN=$NODE_ENROLLMENT_TOKEN"
    -e "DATABASE_URL=$DATABASE_URL"
    -e "REDIS_HOST=$REDIS_HOST"
    -e "REDIS_PORT=$REDIS_PORT"
    -e "REDIS_URL=redis://${REDIS_HOST}:${REDIS_PORT}/12"
    -e RAILS_ENV=production
    -e RACK_ENV=production
    -e DOCKER_CERT_PATH=/root/.docker
    -e CS_SSH_KEY=lib/ssh/id_ed25519
    -e PROXY_IP_DIR=/usr/src/app/lib/proxy_ips
    -e "RAILS_MAX_THREADS=$RAILS_MAX_THREADS"
    -e "RAILS_MIN_THREADS=$RAILS_MIN_THREADS"
    -e "SENTRY_DSN=$SENTRY_DSN"
    -e "MALLOC_CONF=$MALLOC_CONF"
    -e "RUBY_YJIT_ENABLE=$RUBY_YJIT_ENABLE"
  )
}

# Consul is gone from the stack, so v1's /root/.consul mount is not carried
# over. The proxy_ips mount is new (ProxyIpList's persistent store).
build_mount_args()
{
  MOUNT_ARGS=(
    -v "${CS_CERT_PATH}/docker:/root/.docker"
    -v "${CS_BRANDING_PATH}:/usr/src/app/public/assets/custom"
    -v "${CS_SSH_KEYS_PATH}:/usr/src/app/lib/ssh"
    -v "${CS_PROXY_IPS_PATH}:/usr/src/app/lib/proxy_ips"
  )
}

container_exists()
{
  [ -n "$(docker ps -aq -f "name=^${CONTAINER}\$")" ]
}

container_running()
{
  [ -n "$(docker ps -q -f "name=^${CONTAINER}\$")" ]
}

remove_container()
{
  if container_exists; then
    echo "Removing the existing $CONTAINER container..."
    docker stop "$CONTAINER" >/dev/null
    docker rm "$CONTAINER" >/dev/null
  fi
}

# Run a one-off command in the portal image. $1 is the container name, the
# rest is the command. Never named "$CONTAINER" so it cannot collide with a
# running portal.
one_off()
{
  local name="$1"
  shift
  build_env_args
  build_mount_args
  docker run --rm --name "$name" \
    --label com.computestacks.role=system \
    "${MOUNT_ARGS[@]}" \
    "${ENV_ARGS[@]}" \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} \
    --net=host \
    --log-driver=journald \
    "$CS_REG" "$@"
}

# Interactive variant: a tty when one exists, plus the terminal geometry the
# rails console wants.
one_off_tty()
{
  local name="$1"
  shift
  build_env_args
  build_mount_args
  tty_args
  term_size_args
  docker run --rm --name "$name" \
    ${TTY_ARGS[@]+"${TTY_ARGS[@]}"} \
    --label com.computestacks.role=system \
    "${MOUNT_ARGS[@]}" \
    "${ENV_ARGS[@]}" \
    "${TERM_ARGS[@]}" \
    --net=host \
    --log-driver=journald \
    "$CS_REG" "$@"
}

# --- subcommands -----------------------------------------------------------

run()
{
  remove_container
  build_env_args
  build_mount_args
  docker run -d --name "$CONTAINER" \
    --label com.computestacks.role=system \
    "${MOUNT_ARGS[@]}" \
    "${ENV_ARGS[@]}" \
    -e "WEB_CONCURRENCY=$WEB_CONCURRENCY" \
    -e "QUEUE_SYSTEM=$QUEUE_SYSTEM" \
    -e "QUEUE_DEPLOYMENTS=$QUEUE_DEPLOYMENTS" \
    -e "QUEUE_LE=$QUEUE_LE" \
    --net=host \
    --restart=unless-stopped \
    --log-driver=journald \
    "$CS_REG"
}

stop()
{
  if container_running; then
    echo "Stopping $CONTAINER..."
    docker stop "$CONTAINER" >/dev/null
  else
    echo "$CONTAINER is not running."
  fi
}

# Restore with: gzip -d <file> && psql <dbname> < <sqlfile>
#
# Runs as root over the unix socket, which postgres authenticates with `peer`
# against the `root` SUPERUSER role roles/controller creates. No credentials
# on the command line and none in the process table.
database_backup()
{
  mkdir -p "$DB_BACKUPS_PATH"
  local target
  target="${DB_BACKUPS_PATH}/${CS_DB_NAME}-$(date '+%Y%m%d-%H%M%S').sql.gz"
  echo "Creating database backup: $target"
  pg_dump "$CS_DB_NAME" -f "$target" -Z 5
}

migrate()
{
  one_off "${CONTAINER}-migrate" bundle exec rails db:migrate
}

# Backup BEFORE anything else: `set -e` means a failed pg_dump aborts the
# upgrade with the old image still on disk and the old container removable,
# which is the whole point of taking it.
upgrade()
{
  database_backup

  echo "Pulling $CS_REG..."
  docker pull "$CS_REG"

  remove_container

  echo "Running migrations..."
  migrate

  echo "Starting $CONTAINER..."
  run
}

# Greenfield schema load. `db:schema:load` — NOT migrate-from-zero, which is
# not equivalent for this application. Guarded by a sentinel so it can never
# blow away a populated database.
bootstrap_db()
{
  if [ -f "$DB_SENTINEL" ]; then
    echo "cstacks: the database is already provisioned ($DB_SENTINEL)." >&2
    echo "cstacks: refusing to load the schema over an existing database." >&2
    exit 1
  fi
  if container_exists; then
    echo "cstacks: an existing $CONTAINER container is present; 'cstacks stop' first." >&2
    exit 1
  fi

  docker pull "$CS_REG"
  # CS_BOOTSTRAP keeps the webauthn initializer off Setting.hostname, which
  # does not exist yet on an empty schema (config/initializers/webauthn.rb).
  EXTRA_ARGS=(-e CS_BOOTSTRAP=true)
  one_off "${CONTAINER}-bootstrap" bundle exec rails db:schema:load
  EXTRA_ARGS=()

  mkdir -p "$CS_STATE_PATH"
  date --iso-8601=seconds > "$DB_SENTINEL"
  echo "Database provisioned; wrote $DB_SENTINEL"
}

# Apply a bootstrap manifest (controller repo doc/bootstrap_manifest.md).
# DRY_RUN=1 cstacks seed <manifest>  -> prints the diff, writes nothing.
#
# Both DRY_RUN and UPDATE_ADDRESSES have to be forwarded explicitly: `docker
# run` starts a fresh environment, so a variable exported by the caller does
# NOT reach the apply inside the container. UPDATE_ADDRESSES was missing here,
# which made roles/controller_seed's `controller_seed_update_addresses` a
# silent no-op -- the apply always ran with readdressing off, and reported
# `manifest differs from database, database wins` no matter what the operator
# asked for.
seed()
{
  local manifest="${1:-}"
  if [ -z "$manifest" ]; then
    echo "usage: cstacks seed <manifest.yml>   (DRY_RUN=1 for a diff-only run)" >&2
    exit 2
  fi
  if [ ! -f "$manifest" ]; then
    echo "cstacks: manifest not found: $manifest" >&2
    exit 1
  fi

  local absolute
  absolute="$(readlink -f "$manifest")"

  build_env_args
  build_mount_args
  docker run --rm --name "${CONTAINER}-seed" \
    --label com.computestacks.role=system \
    "${MOUNT_ARGS[@]}" \
    "${ENV_ARGS[@]}" \
    -v "${absolute}:/tmp/manifest.yml:ro" \
    -e "DRY_RUN=${DRY_RUN:-}" \
    -e "UPDATE_ADDRESSES=${UPDATE_ADDRESSES:-}" \
    --net=host \
    --log-driver=journald \
    "$CS_REG" bundle exec rake "bootstrap:apply[/tmp/manifest.yml]"
}

# THE cross-role invocation contract (docs/contracts.md). Used by cs_agent's
# enroll task, delegated to this host, to read a node's agent_token_hash.
# Nothing but the ruby program's own output goes to stdout, and its exit code
# is this process's exit code (hence exec, and hence no -t: a tty would inject
# carriage returns into the value the caller parses).
runner()
{
  local program="${1:-}"
  if [ -z "$program" ]; then
    echo "usage: cstacks runner '<ruby>'" >&2
    exit 2
  fi
  if ! container_running; then
    echo "cstacks: the $CONTAINER container is not running; 'cstacks run' first." >&2
    exit 1
  fi
  exec docker exec "$CONTAINER" bin/rails runner "$program"
}

exec_in_portal()
{
  tty_args
  term_size_args
  docker exec ${TTY_ARGS[@]+"${TTY_ARGS[@]}"} "${TERM_ARGS[@]}" "$CONTAINER" "$@"
}

console()
{
  if container_running; then
    exec_in_portal bundle exec rails c
  else
    one_off_tty "${CONTAINER}-console" bundle exec rails c
  fi
}

container()
{
  if container_running; then
    exec_in_portal bash
  else
    one_off_tty "${CONTAINER}-shell" bash
  fi
}

# Controller -> node connectivity (docker mTLS, ssh, agent).
test_connection()
{
  if container_running; then
    exec_in_portal bundle exec rake test_connection:all
  else
    one_off_tty "${CONTAINER}-test" bundle exec rake test_connection:all
  fi
}

portal_logs()
{
  journalctl --all CONTAINER_NAME="$CONTAINER"
}

portal_log_tail()
{
  journalctl --all CONTAINER_NAME="$CONTAINER" -f
}

usage()
{
  cat <<'EOF'
usage: cstacks <command> [args]

where <command> is one of:

  run                     (Re)create and start the portal container
  stop                    Stop the portal container
  upgrade                 Back up the database, pull, migrate, restart
  migrate                 Run pending database migrations
  bootstrap-db            Load the schema on a greenfield database (once)
  seed <manifest.yml>     Apply a bootstrap manifest (DRY_RUN=1 for a diff)
  runner '<ruby>'         Run ruby in the portal container (stdout passthrough)
  console                 Rails console
  container               Shell inside the portal container
  test                    Test controller -> node connectivity
  database-backup         pg_dump the controller database
  logs                    Controller logs
  tail-logs               Follow controller logs
  help                    This message

Settings are read from /etc/default/computestacks.
EOF
}

case "${1:-}" in
  run)              run ;;
  stop)             stop ;;
  upgrade)          upgrade ;;
  migrate)          migrate ;;
  bootstrap-db)     bootstrap_db ;;
  seed)             seed "${2:-}" ;;
  runner)           runner "${2:-}" ;;
  console)          console ;;
  container)        container ;;
  test)             test_connection ;;
  database-backup)  database_backup ;;
  logs)             portal_logs ;;
  tail-logs)        portal_log_tail ;;
  -h|-help|--help|help) usage ;;
  "")               usage; exit 1 ;;
  *)                echo "cstacks: unknown command '$1'" >&2; usage >&2; exit 1 ;;
esac
