#!/usr/bin/env bash
# scale-control-plane.sh — size and tune the Agent Substrate cluster for the
# 1,000-agent keynote demo. Reproduces the configuration that was measured on
# 2026-09-26 (see ../../README.md, "Results").
#
# Idempotent: every step compares the live value first and only changes what is
# different. On an already-configured cluster it changes nothing.
#   DRY_RUN=1   print what would change, change nothing.
#
# What it does (in order):
#   1. Node pools: substrate-node-pool = 25 x c3-standard-4 (created by
#      setup-gcp bootstrap); keynote-driver-pool = 4 x n2-standard-8 with label
#      pool=keynote-driver and taint dedicated=keynote-driver:NoSchedule
#      (created here if missing). The control plane (Postgres, ate-api,
#      atenet) and the keynote driver run on keynote-driver-pool so they never
#      compete with the 1,600 sandbox workers for CPU.
#   2. Labels the substrate-node-pool nodes ate.dev/substrate-version=v0.1.0-gke.1
#      (the atelet DaemonSet, WorkerPool and ate-node-tuner select on it).
#   3. Postgres: settings below, pinned to one keynote-driver-pool node, 2-16 CPU.
#      WARNING: fsync=off and full_page_writes=off trade crash safety for speed —
#      a node crash can corrupt the Substrate database. Demo clusters only.
#   4. ate-api-server: exactly ONE replica, pinned to the Postgres node, DB pool
#      160 max / 64 min connections. The patched ate-api keeps in-memory
#      template/worker caches that assume a single replica — do not scale it out.
#   5. atenet-router and atenet-egress: 4 replicas each on keynote-driver-pool.
#   6. podcertificate-controller: WORKERS_PER_SIGNER=16 (signs the 1,600 worker
#      pod certificates quickly when the WorkerPool scales).
#   7. WorkerPool/sandbox-workerpool: see sandbox-workerpool.yaml (applied here).
#
# Usage:
#   PROJECT_ID=my-project ZONE=asia-northeast1-b SUBSTRATE_CLUSTER=my-substrate \
#     ./scale-control-plane.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
ZONE="${ZONE:?set ZONE}"
SUBSTRATE_CLUSTER="${SUBSTRATE_CLUSTER:?set SUBSTRATE_CLUSTER}"
CTX="${CTX_SUB:-gke_${PROJECT_ID}_${ZONE}_${SUBSTRATE_CLUSTER}}"
DRY_RUN="${DRY_RUN:-0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
K() { kubectl --context="${CTX}" "$@"; }
say() { echo "=== $*"; }
act() { if [ "${DRY_RUN}" = 1 ]; then echo "    [dry-run] would run: $*"; else "$@"; fi; }
# Patch only if the server-side result differs from the live object.
patch_if_needed() {  # $1=ns $2=kind/name $3=type $4=patch
  local ns=$1 obj=$2 type=$3 patch=$4 before after
  before=$(K -n "$ns" get "$obj" -o json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d["spec"],sort_keys=True))')
  after=$(K -n "$ns" patch "$obj" --type="$type" -p "$patch" --dry-run=server -o json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d["spec"],sort_keys=True))')
  if [ "$before" = "$after" ]; then echo "    $ns/$obj: unchanged"; else echo "    $ns/$obj: CHANGING"; act K -n "$ns" patch "$obj" --type="$type" -p "$patch"; fi
}

WORKER_NODES=25
CP_NODES=4
PG_SETTINGS=(
  "max_connections = 1000"
  "shared_buffers = 4GB"
  "work_mem = 32MB"
  "synchronous_commit = off"
  "wal_buffers = 16MB"
  "checkpoint_completion_target = 0.9"
  "max_wal_size = 4GB"
  "fsync = off"
  "full_page_writes = off"
  "autovacuum_naptime = 5s"
  "commit_delay = 0"
)
CP_TOLERATION='[{"effect":"NoSchedule","key":"dedicated","operator":"Equal","value":"keynote-driver"}]'
CP_AFFINITY='{"nodeAffinity":{"preferredDuringSchedulingIgnoredDuringExecution":[{"weight":100,"preference":{"matchExpressions":[{"key":"cloud.google.com/gke-nodepool","operator":"In","values":["keynote-driver-pool"]}]}}]}}'

say "1. Node pools"
nodes_in() { K get nodes -l "cloud.google.com/gke-nodepool=$1" --no-headers 2>/dev/null | wc -l; }
cur=$(nodes_in substrate-node-pool)
if [ "$cur" -eq "$WORKER_NODES" ]; then echo "    substrate-node-pool: ${cur} nodes (ok)"; else
  echo "    substrate-node-pool: ${cur} -> ${WORKER_NODES} nodes"
  act gcloud container clusters resize "${SUBSTRATE_CLUSTER}" --project="${PROJECT_ID}" --zone="${ZONE}" --node-pool=substrate-node-pool --num-nodes="${WORKER_NODES}" --quiet
fi
cur=$(nodes_in keynote-driver-pool)
if [ "$cur" -eq 0 ]; then
  echo "    keynote-driver-pool: creating ${CP_NODES} x n2-standard-8"
  act gcloud container node-pools create keynote-driver-pool --project="${PROJECT_ID}" --zone="${ZONE}" --cluster="${SUBSTRATE_CLUSTER}" \
    --machine-type=n2-standard-8 --num-nodes="${CP_NODES}" --disk-type=pd-balanced --disk-size=50 \
    --node-labels=pool=keynote-driver --node-taints=dedicated=keynote-driver:NoSchedule
elif [ "$cur" -eq "$CP_NODES" ]; then echo "    keynote-driver-pool: ${cur} nodes (ok)"; else
  echo "    keynote-driver-pool: ${cur} -> ${CP_NODES} nodes"
  act gcloud container clusters resize "${SUBSTRATE_CLUSTER}" --project="${PROJECT_ID}" --zone="${ZONE}" --node-pool=keynote-driver-pool --num-nodes="${CP_NODES}" --quiet
fi

say "2. Substrate node labels"
for node in $(K get nodes -l cloud.google.com/gke-nodepool=substrate-node-pool -o jsonpath='{.items[*].metadata.name}'); do
  v=$(K get node "$node" -o jsonpath='{.metadata.labels.ate\.dev/substrate-version}')
  [ "$v" = "v0.1.0-gke.1" ] || act K label node "$node" ate.dev/substrate-version=v0.1.0-gke.1 --overwrite
done
echo "    $(K get nodes -l ate.dev/substrate-version=v0.1.0-gke.1 --no-headers | wc -l) nodes labelled"

say "3. Postgres"
PG_NODE=$(K -n ate-system get pod postgres-0 -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)
if [ -z "$PG_NODE" ] || [ "$(K get node "$PG_NODE" -o jsonpath='{.metadata.labels.cloud\.google\.com/gke-nodepool}')" != keynote-driver-pool ]; then
  PG_NODE=$(K get nodes -l cloud.google.com/gke-nodepool=keynote-driver-pool -o jsonpath='{.items[0].metadata.name}')
fi
echo "    control-plane node for Postgres + ate-api: ${PG_NODE}"
K -n ate-system get cm postgres-config -o json > /tmp/.pgcm.$$.json
python3 - "$(printf '%s\n' "${PG_SETTINGS[@]}")" /tmp/.pgcm.$$.json > /tmp/.pgcm.$$.new.json <<'EOF'
import json, re, sys
settings = [l for l in sys.argv[1].splitlines() if l.strip()]
cm = json.load(open(sys.argv[2]))
lines = cm["data"]["postgresql.conf"].rstrip("\n").split("\n")
for s in settings:
    key = s.split("=")[0].strip()
    idx = [i for i, l in enumerate(lines) if re.match(r"\s*#?\s*%s\s*=" % re.escape(key), l)]
    if idx:
        lines[idx[0]] = s
        for i in reversed(idx[1:]): del lines[i]
    else:
        lines.append(s)
cm["data"]["postgresql.conf"] = "\n".join(lines) + "\n"
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields", "generation"):
    cm["metadata"].pop(k, None)
json.dump(cm, sys.stdout)
EOF
PG_CHANGED=0
if python3 -c 'import json,sys; a=json.load(open(sys.argv[1]))["data"]["postgresql.conf"]; b=json.load(open(sys.argv[2]))["data"]["postgresql.conf"]; sys.exit(0 if a==b else 1)' /tmp/.pgcm.$$.json /tmp/.pgcm.$$.new.json; then
  echo "    postgres-config: unchanged"
else
  echo "    postgres-config: CHANGING"; PG_CHANGED=1
  act K -n ate-system replace -f /tmp/.pgcm.$$.new.json
fi
rm -f /tmp/.pgcm.$$.json /tmp/.pgcm.$$.new.json
PG_SPEC_BEFORE=$(K -n ate-system get sts postgres -o jsonpath='{.metadata.generation}')
patch_if_needed ate-system sts/postgres strategic "{\"spec\":{\"template\":{\"spec\":{
  \"nodeSelector\":{\"cloud.google.com/gke-nodepool\":\"keynote-driver-pool\",\"kubernetes.io/hostname\":\"${PG_NODE}\"},
  \"tolerations\":${CP_TOLERATION},
  \"containers\":[{\"name\":\"postgres\",\"resources\":{\"requests\":{\"cpu\":\"2\",\"memory\":\"2Gi\"},\"limits\":{\"cpu\":\"16\",\"memory\":\"8Gi\"}}}]}}}}"
if [ "${PG_CHANGED}" = 1 ] && [ "${DRY_RUN}" != 1 ] && [ "$(K -n ate-system get sts postgres -o jsonpath='{.metadata.generation}')" = "${PG_SPEC_BEFORE}" ]; then
  # Config changed but the pod template did not, so nothing restarted Postgres.
  if K -n ate-system exec postgres-0 -c postgres -- sh -c 'pidof http_srv' >/dev/null 2>&1 && [ "${FORCE:-0}" != 1 ]; then
    echo "    !! postgres-0 also runs the patched-binary file server (deploy-patched-binaries.sh)."
    echo "    !! Restarting it stops that server. Re-run with FORCE=1, then re-run deploy-patched-binaries.sh."
    exit 1
  fi
  K -n ate-system delete pod postgres-0 --wait=true
fi
[ "${DRY_RUN}" = 1 ] || K -n ate-system wait --for=condition=Ready pod/postgres-0 --timeout=180s

say "4. ate-api-server (1 replica, DB pool 160/64, on ${PG_NODE})"
DSN='postgresql://postgres@postgres.ate-system.svc:5432/atepg?sslmode=verify-full&sslrootcert=/run/servicedns.podcert.ate.dev/trust-bundle.pem&sslcert=/run/podidentity.podcert.ate.dev/credential-bundle.pem&sslkey=/run/podidentity.podcert.ate.dev/credential-bundle.pem&pool_max_conns=160&pool_min_conns=64'
if [ "$(K -n ate-system get cm ate-api-server-envvars -o jsonpath='{.data.ATE_API_POSTGRES_CONNECTION_STRING}')" = "$DSN" ]; then
  echo "    ate-api-server-envvars: unchanged"
else
  echo "    ate-api-server-envvars: CHANGING"
  act K -n ate-system patch cm ate-api-server-envvars --type=merge -p "{\"data\":{\"ATE_API_POSTGRES_CONNECTION_STRING\":\"${DSN}\"}}"
  [ "${DRY_RUN}" = 1 ] || K -n ate-system rollout restart deploy/ate-api-server
fi
patch_if_needed ate-system deploy/ate-api-server strategic "{\"spec\":{\"replicas\":1,\"template\":{\"spec\":{
  \"nodeSelector\":{\"kubernetes.io/hostname\":\"${PG_NODE}\"},\"tolerations\":${CP_TOLERATION},\"affinity\":${CP_AFFINITY}}}}}"

say "5. atenet-router / atenet-egress (4 replicas each)"
for d in atenet-router atenet-egress; do
  patch_if_needed ate-system "deploy/$d" strategic "{\"spec\":{\"replicas\":4,\"template\":{\"spec\":{\"tolerations\":${CP_TOLERATION},\"affinity\":${CP_AFFINITY}}}}}"
done

say "6. podcertificate-controller WORKERS_PER_SIGNER=16"
if [ "$(K -n podcertificate-controller-system get deploy podcertificate-controller -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="WORKERS_PER_SIGNER")].value}')" = 16 ]; then
  echo "    unchanged"
else
  act K -n podcertificate-controller-system set env deploy/podcertificate-controller WORKERS_PER_SIGNER=16
fi

say "7. WorkerPool/sandbox-workerpool (1,600 dense workers)"
if [ "${DRY_RUN}" = 1 ]; then K diff -f "${HERE}/sandbox-workerpool.yaml" || true; else K apply -f "${HERE}/sandbox-workerpool.yaml"; fi

if [ "${DRY_RUN}" != 1 ]; then
  say "Waiting for rollouts"
  K -n ate-system rollout status deploy/ate-api-server --timeout=300s
  K -n ate-system rollout status deploy/atenet-router --timeout=300s
  K -n ate-system rollout status deploy/atenet-egress --timeout=300s
  K -n ate-demo-sandbox rollout status deploy/sandbox-workerpool --timeout=900s
fi
say "done"
