#!/usr/bin/bash
#
# ComputeStacks management script
#

set -e

. /etc/default/computestacks

bootstrap-app()
{
  if [ "$(docker ps -aq -f name=portal)" ]; then
    echo "Existing container found, aborting..."
    exit 1
  fi
  docker run -it --rm --name portal \
          --label com.computestacks.role=system \
          -v $CS_CERT_PATH/docker:/root/.docker:Z \
          -v $CS_CERT_PATH/consul:/root/.consul:Z \
          -v $CS_RAKE_PATH/bootstrap.rake:/usr/src/app/lib/tasks/bootstrap.rake:Z \
          -e APP_ID=$CS_APP_ID \
          -e CURRENCY=$CS_CURRENCY \
          -e LOCALE=$CS_LOCALE \
          -e SECRET_KEY_BASE=$SECRET_KEY_BASE \
          -e USER_AUTH_SECRET=$USER_AUTH_SECRET \
          -e DATABASE_URL=$DATABASE_URL \
          -e REDIS_HOST=$REDIS_HOST \
          -e REDIS_URL=redis://$REDIS_HOST:6379/12 \
          -e RAILS_ENV=production \
          -e RACK_ENV=production \
          -e DOCKER_CERT_PATH=/root/.docker \
          -e CS_SSH_KEY=/usr/src/app/lib/ssh/id_ed25519 \
          -e CS_BOOTSTRAP=true \
          -e SENTRY_DSN=$SENTRY_DSN \
          --net=host \
          --log-driver=journald \
          $CS_REG bundle exec rake bootstrap
}

bootstrap-db()
{
  if [ "$(docker ps -aq -f name=portal)" ]; then
    echo "Existing container found, aborting..."
    exit 1
  fi
  docker pull $CS_REG
  docker run -it --rm --name portal \
        --label com.computestacks.role=system \
        -v $CS_CERT_PATH/docker:/root/.docker:Z \
        -v $CS_CERT_PATH/consul:/root/.consul:Z \
        -e APP_ID=$CS_APP_ID \
        -e CURRENCY=$CS_CURRENCY \
        -e LOCALE=$CS_LOCALE \
        -e SECRET_KEY_BASE=$SECRET_KEY_BASE \
        -e USER_AUTH_SECRET=$USER_AUTH_SECRET \
        -e DATABASE_URL=$DATABASE_URL \
        -e REDIS_HOST=$REDIS_HOST \
        -e REDIS_URL=redis://$REDIS_HOST:6379/12 \
        -e RAILS_ENV=production \
        -e RACK_ENV=production \
        -e DOCKER_CERT_PATH=/root/.docker \
        -e CS_SSH_KEY=/usr/src/app/lib/ssh/id_ed25519 \
        -e CS_BOOTSTRAP=true \
        -e SENTRY_DSN=$SENTRY_DSN \
        --net=host \
        --log-driver=journald \
        $CS_REG bundle exec rails db:schema:load \
  && touch $CS_RAKE_PATH/.db_provisioned
}

console()
{
  if [ "$(docker ps -q -f name=portal)" ]; then
    docker exec -e COLUMNS="`tput cols`" -e LINES="`tput lines`" -it portal bundle exec rails c
  else
    docker run -it --rm --name portal \
        --label com.computestacks.role=system \
        -v $CS_CERT_PATH/docker:/root/.docker:Z \
        -v $CS_CERT_PATH/consul:/root/.consul:Z \
        -v $CS_BRANDING_PATH:/usr/src/app/public/assets/custom:Z \
        -v $CS_SSH_KEYS_PATH:/usr/src/app/lib/ssh:Z \
        -e APP_ID=$CS_APP_ID \
        -e CURRENCY=$CS_CURRENCY \
        -e LOCALE=$CS_LOCALE \
        -e SECRET_KEY_BASE=$SECRET_KEY_BASE \
        -e USER_AUTH_SECRET=$USER_AUTH_SECRET \
        -e DATABASE_URL=$DATABASE_URL \
        -e REDIS_HOST=$REDIS_HOST \
        -e REDIS_URL=redis://$REDIS_HOST:6379/12 \
        -e RAILS_ENV=production \
        -e RACK_ENV=production \
        -e DOCKER_CERT_PATH=/root/.docker \
        -e CS_SSH_KEY=/usr/src/app/lib/ssh/id_ed25519 \
        -e RAILS_MIN_THREADS=$RAILS_MIN_THREADS \
        -e RAILS_MAX_THREADS=$RAILS_MAX_THREADS \
        -e COLUMNS="`tput cols`" \
        -e LINES="`tput lines`" \
        -e SENTRY_DSN=$SENTRY_DSN \
        --net=host \
        --log-driver=journald \
        $CS_REG bundle exec rails c
  fi
}

container()
{
  if [ "$(docker ps -q -f name=portal)" ]; then
    docker exec -e COLUMNS="`tput cols`" -e LINES="`tput lines`" -it portal bash
  else
    docker run -it --rm --name portal \
        --label com.computestacks.role=system \
        -v $CS_CERT_PATH/docker:/root/.docker:Z \
        -v $CS_CERT_PATH/consul:/root/.consul:Z \
        -v $CS_BRANDING_PATH:/usr/src/app/public/assets/custom:Z \
        -v $CS_SSH_KEYS_PATH:/usr/src/app/lib/ssh:Z \
        -e APP_ID=$CS_APP_ID \
        -e CURRENCY=$CS_CURRENCY \
        -e LOCALE=$CS_LOCALE \
        -e SECRET_KEY_BASE=$SECRET_KEY_BASE \
        -e USER_AUTH_SECRET=$USER_AUTH_SECRET \
        -e DATABASE_URL=$DATABASE_URL \
        -e REDIS_HOST=$REDIS_HOST \
        -e REDIS_URL=redis://$REDIS_HOST:6379/12 \
        -e RAILS_ENV=production \
        -e RACK_ENV=production \
        -e DOCKER_CERT_PATH=/root/.docker \
        -e CS_SSH_KEY=/usr/src/app/lib/ssh/id_ed25519 \
        -e RAILS_MIN_THREADS=$RAILS_MIN_THREADS \
        -e RAILS_MAX_THREADS=$RAILS_MAX_THREADS \
        -e COLUMNS="`tput cols`" \
        -e LINES="`tput lines`" \
        -e SENTRY_DSN=$SENTRY_DSN \
        --net=host \
        --log-driver=journald \
        $CS_REG bash
  fi
}

# Restore with gzip -d <filename> && psql <dbname> < <sqlfile>
database-backup()
{
  echo "Creating database backup..."
  pg_dump cloudportal -f $DB_BACKUPS_PATH/cloudportal-$(date '+%Y%m%d-%H%M_%S').sql.gz -Z 5
}

upgrade()
{
  echo "Pulling latest image..."
  docker pull $CS_REG

  if [ "$(docker ps -aq -f name=portal)" ]; then
    echo "Existing container found, removing..."
    docker stop portal && docker rm portal
  fi

  database-backup

  echo "Running migrations..."
  docker run -it --rm --name portal \
          --label com.computestacks.role=system \
          -v $CS_CERT_PATH/docker:/root/.docker:Z \
          -v $CS_CERT_PATH/consul:/root/.consul:Z \
          -e APP_ID=$CS_APP_ID \
          -e CURRENCY=$CS_CURRENCY \
          -e LOCALE=$CS_LOCALE \
          -e SECRET_KEY_BASE=$SECRET_KEY_BASE \
          -e USER_AUTH_SECRET=$USER_AUTH_SECRET \
          -e DATABASE_URL=$DATABASE_URL \
          -e REDIS_HOST=$REDIS_HOST \
          -e REDIS_URL=redis://$REDIS_HOST:6379/12 \
          -e RAILS_ENV=production \
          -e RACK_ENV=production \
          -e DOCKER_CERT_PATH=/root/.docker \
          -e CS_SSH_KEY=/usr/src/app/lib/ssh/id_ed25519 \
          -e SENTRY_DSN=$SENTRY_DSN \
          --net=host \
          --log-driver=journald \
          $CS_REG bundle exec rails db:migrate
}

run()
{
  if [ "$(docker ps -aq -f name=portal)" ]; then
    echo "Existing container found, removing..."
    docker stop portal && docker rm portal
  fi
  docker run -d --name portal \
        --label com.computestacks.role=system \
        -v $CS_CERT_PATH/docker:/root/.docker:Z \
        -v $CS_CERT_PATH/consul:/root/.consul:Z \
        -v $CS_BRANDING_PATH:/usr/src/app/public/assets/custom:Z \
        -v $CS_SSH_KEYS_PATH:/usr/src/app/lib/ssh:Z \
        -e APP_ID=$CS_APP_ID \
        -e CURRENCY=$CS_CURRENCY \
        -e LOCALE=$CS_LOCALE \
        -e SECRET_KEY_BASE=$SECRET_KEY_BASE \
        -e USER_AUTH_SECRET=$USER_AUTH_SECRET \
        -e DATABASE_URL=$DATABASE_URL \
        -e REDIS_HOST=$REDIS_HOST \
        -e REDIS_URL=redis://$REDIS_HOST:6379/12 \
        -e RAILS_ENV=production \
        -e RACK_ENV=production \
        -e DOCKER_CERT_PATH=/root/.docker \
        -e CS_SSH_KEY=/usr/src/app/lib/ssh/id_ed25519 \
        -e RAILS_MIN_THREADS=$RAILS_MIN_THREADS \
        -e RAILS_MAX_THREADS=$RAILS_MAX_THREADS \
        -e QUEUE_SYSTEM=$QUEUE_SYSTEM \
        -e QUEUE_DEPLOYMENTS=$QUEUE_DEPLOYMENTS \
        -e QUEUE_LE=$QUEUE_LE \
        -e SENTRY_DSN=$SENTRY_DSN \
        --net=host \
        --restart=unless-stopped \
        --log-driver=journald \
        $CS_REG
}

# Test connectivity
test()
{
  if [ "$(docker ps -q -f name=portal)" ]; then
    docker exec -e COLUMNS="`tput cols`" -e LINES="`tput lines`" -it portal bundle exec rake test_connection:all
  else
    docker run -it --rm --name portal \
        --label com.computestacks.role=system \
        -v $CS_CERT_PATH/docker:/root/.docker:Z \
        -v $CS_CERT_PATH/consul:/root/.consul:Z \
        -v $CS_BRANDING_PATH:/usr/src/app/public/assets/custom:Z \
        -v $CS_SSH_KEYS_PATH:/usr/src/app/lib/ssh:Z \
        -e APP_ID=$CS_APP_ID \
        -e CURRENCY=$CS_CURRENCY \
        -e LOCALE=$CS_LOCALE \
        -e SECRET_KEY_BASE=$SECRET_KEY_BASE \
        -e USER_AUTH_SECRET=$USER_AUTH_SECRET \
        -e DATABASE_URL=$DATABASE_URL \
        -e REDIS_HOST=$REDIS_HOST \
        -e REDIS_URL=redis://$REDIS_HOST:6379/12 \
        -e RAILS_ENV=production \
        -e RACK_ENV=production \
        -e DOCKER_CERT_PATH=/root/.docker \
        -e CS_SSH_KEY=/usr/src/app/lib/ssh/id_ed25519 \
        -e RAILS_MIN_THREADS=$RAILS_MIN_THREADS \
        -e RAILS_MAX_THREADS=$RAILS_MAX_THREADS \
        -e COLUMNS="`tput cols`" \
        -e LINES="`tput lines`" \
        -e SENTRY_DSN=$SENTRY_DSN \
        --net=host \
        --log-driver=journald \
        $CS_REG bundle exec rake test_connection:all
  fi
}

portal-logs()
{
  journalctl --all CONTAINER_NAME=portal
}

portal-log-tail()
{
  journalctl --all CONTAINER_NAME=portal -f
}

usage()
{
  echo "usage: cstacks <command>

where COMMAND is one of:

  bootstrap-app     Initialize the ComputeStacks application
  bootstrap-db      Create the database
  console           Enter the ComputeStacks console
  container         Enter the ComputeStacks application container
  database-backup   Create a database backup (postgres)
  help              Usage Help
  logs              Controller Logs
  tail-logs         Follow controller logs
  run               Run ComputeStacks
  upgrade           Pull the latest version and run any database migrations
  test              Test connectivity between controller and nodes

"
}

while [ "$1" != "" ]; do
  case $1 in
    bootstrap-app )           bootstrap-app
                              exit
                              ;;
    bootstrap-db )            bootstrap-db
                              exit
                              ;;
    database-backup )         database-backup
                              exit
                              ;;
    console )                 console
                              exit
                              ;;
    container )               container
                              exit
                              ;;
    upgrade )                 upgrade
                              exit
                              ;;
    logs )                    portal-logs
                              exit
                              ;;
    tail-logs )               portal-log-tail
                              exit
                              ;;
    test )                    test
                              exit
                              ;;
    -h|-help|--help|help )    usage
                              exit
                              ;;
    run )                     run
                              exit
                              ;;
    * )                       usage
                              exit 1
  esac
done

if [ "$!" == "" ]; then
  usage
  exit
fi
