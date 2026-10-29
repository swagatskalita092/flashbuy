#!/usr/bin/env sh
# Pytest against throwaway Postgres/Redis. Does not start app1/app2/app3.
set -eu
cd "$(dirname "$0")/.."
status=0
docker compose --profile test run --rm --build test-runner || status=$?
docker compose --profile test rm -sfv postgres-test redis-test
exit $status
