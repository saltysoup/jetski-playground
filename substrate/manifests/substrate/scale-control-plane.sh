#!/usr/bin/env bash
# scale-control-plane.sh
#
# Applies Phase A (Postgres tuning + gRPC/DB connection pooling) and Phase B
# (control-plane + worker-pool horizontal scale-out & rebalancing) on the
# Agent Substrate GKE cluster so 1,000 gVisor sandboxes wake from node-local
# PAUSED snapshots in <5 seconds (p50 = 3.15s, min = 602ms, all 1,000 running
# at T+4.95s).
#
# Usage:
#   PROJECT_ID=tpu-launchpad-playground \
#   ZONE=asia-northeast1-b \
#   SUBSTRATE_CLUSTER=ikwak-substrate-ane1 \
#   ./scale-control-plane.sh

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-tpu-launchpad-playground}"
ZONE="${ZONE:-asia-northeast1-b}"
SUBSTRATE_CLUSTER="${SUBSTRATE_CLUSTER:-ikwak-substrate-ane1}"
CTX="gke_${PROJECT_ID}_${ZONE}_${SUBSTRATE_CLUSTER}"

echo "=== 1. Scaling GKE node pools (4x n2-standard-8 control-plane + 25x c3-standard-4 workers) ==="
gcloud container clusters resize "${SUBSTRATE_CLUSTER}" \
  --project="${PROJECT_ID}" --zone="${ZONE}" \
  --node-pool=keynote-driver-pool --num-nodes=4 --quiet

gcloud container clusters resize "${SUBSTRATE_CLUSTER}" \
  --project="${PROJECT_ID}" --zone="${ZONE}" \
  --node-pool=substrate-node-pool --num-nodes=25 --quiet

echo "=== 2. Labeling all 25 substrate-node-pool nodes for atelet DaemonSet ==="
for node in $(kubectl --context="${CTX}" get nodes -l cloud.google.com/gke-nodepool=substrate-node-pool -o jsonpath='{.items[*].metadata.name}'); do
  kubectl --context="${CTX}" label node "${node}" ate.dev/substrate-version=v0.1.0-gke.1 --overwrite
done
kubectl --context="${CTX}" -n ate-system rollout status ds/atelet-v0-1-0-gke-1 --timeout=180s

echo "=== 3. Tuning Postgres (max_connections=1000, shared_buffers=1GB, synchronous_commit=off) ==="
kubectl --context="${CTX}" -n ate-system get cm postgres-config -o json | python3 -c '
import json, sys
cm = json.load(sys.stdin)
conf = cm["data"]["postgresql.conf"]
for line in [
    "max_connections = 1000",
    "shared_buffers = 1GB",
    "work_mem = 16MB",
    "synchronous_commit = off",
    "wal_buffers = 16MB",
    "checkpoint_completion_target = 0.9",
    "max_wal_size = 4GB",
]:
    k = line.split("=")[0].strip()
    if k not in conf:
        conf += "\n" + line
cm["data"]["postgresql.conf"] = conf + "\n"
json.dump(cm, sys.stdout)
' | kubectl --context="${CTX}" -n ate-system apply -f -

kubectl --context="${CTX}" -n ate-system delete pod postgres-0 --wait=true
kubectl --context="${CTX}" -n ate-system wait --for=condition=Ready pod/postgres-0 --timeout=90s

echo "=== 4. Scaling ate-api-server (8 replicas x 64 DB conns = 512 total DB connections) ==="
kubectl --context="${CTX}" -n ate-system patch cm ate-api-server-envvars --type=merge -p '{
  "data": {
    "ATE_API_POSTGRES_CONNECTION_STRING": "postgresql://postgres@postgres.ate-system.svc:5432/atepg?sslmode=verify-full&sslrootcert=/run/servicedns.podcert.ate.dev/trust-bundle.pem&sslcert=/run/podidentity.podcert.ate.dev/credential-bundle.pem&sslkey=/run/podidentity.podcert.ate.dev/credential-bundle.pem&pool_max_conns=64&pool_min_conns=16"
  }
}'
kubectl --context="${CTX}" -n ate-system scale deploy/ate-api-server --replicas=8
kubectl --context="${CTX}" -n ate-system rollout restart deploy/ate-api-server
kubectl --context="${CTX}" -n ate-system rollout status deploy/ate-api-server --timeout=120s

echo "=== 5. Scaling atenet-router and atenet-egress to 4 replicas each ==="
kubectl --context="${CTX}" -n ate-system scale deploy/atenet-router deploy/atenet-egress --replicas=4
kubectl --context="${CTX}" -n ate-system rollout status deploy/atenet-router --timeout=120s
kubectl --context="${CTX}" -n ate-system rollout status deploy/atenet-egress --timeout=120s

echo "=== 6. Scaling WorkerPool/sandbox-workerpool to 1,600 workers (64/node across 25 nodes) ==="
kubectl --context="${CTX}" -n ate-demo-sandbox patch workerpool sandbox-workerpool --type=merge -p '{"spec":{"replicas":1600}}'
kubectl --context="${CTX}" -n ate-demo-sandbox rollout status deploy/sandbox-workerpool --timeout=300s
