#!/usr/bin/env bash
# Chaos drill: freeze the current admission leader instead of killing it.
#
# The existing Phase 5B drills (kill a replica, kill Redis, kill Postgres)
# all prove correctness when a process dies outright. This proves
# correctness when a process does NOT die, it just stops responding for
# longer than its lease and then comes back, which is what a GC pause or
# a frozen VM actually looks like in production, and is exactly the gap
# the fencing token was built to close.
#
# Compose publishes the Caddy load balancer on host port 8000 (not 8080).
#
# Usage: run this while a real Locust load test is hitting the 3-replica
# stack through Caddy, same as the existing Phase 5B drills, then watch
# order counts and idempotency keys afterward for duplicates.
set -euo pipefail

echo "Checking current admission leader via /health..."
curl -s http://localhost:8000/health
echo ""

read -rp "Enter the container name of the current leader (see instance_id above, match it to a container with docker ps): " CONTAINER

echo "Pausing $CONTAINER (cgroup freeze, equivalent to kill -STOP for every process in it)..."
docker pause "$CONTAINER"

echo "Frozen. Waiting past the lease TTL (default 5s) plus a safety margin..."
sleep 8

echo "Leadership should have moved to a different replica by now:"
curl -s http://localhost:8000/health
echo ""

echo "Un-pausing $CONTAINER..."
docker unpause "$CONTAINER"

echo "Resumed. Checking /health again, both instances should not claim leadership:"
sleep 1
curl -s http://localhost:8000/health
echo ""

echo "Now check the real results:"
echo "  - No duplicate idempotency keys in the orders table"
echo "  - Total admitted/outstanding tokens should match what a single leader"
echo "    would have admitted across the whole drill, not double"
echo "  - Compare against the Phase 5B results table style in docs/failure-modes.md"
