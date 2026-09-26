#!/usr/bin/env bash
# deploy-patched-binaries.sh — run the patched ate-api and atelet (built from
# agent-substrate/substrate@fa6d949 + ../../patches/ateapi-atelet-fast-wake.patch)
# on top of the stock GKE release images (v0.1.0-gke.1).
#
# How it works (this is exactly how the measured cluster runs):
#   1. A tiny static file server (http_srv.go) runs inside postgres-0 on :18888
#      and serves /var/lib/postgresql/data, where bin_ateapi.gz / bin_atelet.gz
#      are uploaded.
#   2. ate-api-server gets a `fetch-bin` init container that downloads
#      bin_ateapi.gz into an emptyDir, and its command becomes /opt/bin/ateapi.
#   3. The atelet DaemonSet gets a `fetch-bin` init container that downloads
#      bin_atelet.gz to the node (hostPath /var/lib/ateom-gvisor/bin_atelet), and
#      its command becomes that file. The download goes to a temp file and is
#      renamed only on success; a failed download fails the init container, so
#      the atelet pod retries instead of starting.
#
# SECURITY WARNING: step 1 exposes the entire Postgres data directory (raw
# database files) unauthenticated on the pod network. Demo clusters only; for
# anything else, bake the binaries into your own images instead.
# FRAGILE: the URLs contain postgres-0's pod IP, and the server lives in /tmp of
# the running container. After postgres-0 restarts, re-run this script BEFORE any
# atelet or ate-api pod restarts (ate-api cannot start without its download).
#
# Idempotent: uploads only if the checksum differs, patches only if the spec
# differs, and restarts atelet / ate-api only when their binary or spec changed.
#   DRY_RUN=1   report only.
#
# Usage: BIN_DIR=/path/with/bin_ateapi.gz+bin_atelet.gz+http_srv CTX_SUB=<kube-context> ./deploy-patched-binaries.sh
set -euo pipefail
CTX="${CTX_SUB:?set CTX_SUB to the Substrate cluster kube context}"
BIN_DIR="${BIN_DIR:?set BIN_DIR}"
DRY_RUN="${DRY_RUN:-0}"
DATA=/var/lib/postgresql/data
K() { kubectl --context="${CTX}" "$@"; }
PGX() { K -n ate-system exec postgres-0 -c postgres -- "$@"; }
say() { echo "=== $*"; }
for f in bin_ateapi.gz bin_atelet.gz http_srv; do [ -s "${BIN_DIR}/$f" ] || { echo "missing ${BIN_DIR}/$f"; exit 1; }; done

PG_IP=$(K -n ate-system get pod postgres-0 -o jsonpath='{.status.podIP}')
URL="http://${PG_IP}:18888"
say "file server: ${URL} (inside postgres-0)"

# 1. File server
if PGX sh -c 'pidof http_srv' >/dev/null 2>&1; then
  echo "    http_srv already running (left as is)"
elif [ "${DRY_RUN}" = 1 ]; then
  echo "    [dry-run] would copy and start http_srv"
else
  K -n ate-system cp -c postgres "${BIN_DIR}/http_srv" ate-system/postgres-0:/tmp/http_srv
  PGX sh -c 'chmod +x /tmp/http_srv && (setsid nohup /tmp/http_srv >/dev/null 2>&1 &) && sleep 1 && pidof http_srv'
fi

# 2. Upload binaries (verified by sha256, atomic rename)
CHANGED=""
for f in bin_ateapi.gz bin_atelet.gz; do
  want=$(sha256sum "${BIN_DIR}/$f" | cut -d' ' -f1)
  have=$(PGX sh -c "sha256sum ${DATA}/$f 2>/dev/null | cut -d' ' -f1" || true)
  if [ "$want" = "$have" ]; then echo "    $f: unchanged (${want:0:12})"; continue; fi
  echo "    $f: ${have:0:12} -> ${want:0:12}"
  CHANGED="$CHANGED $f"
  [ "${DRY_RUN}" = 1 ] && continue
  for try in 1 2 3; do
    K -n ate-system cp -c postgres "${BIN_DIR}/$f" "ate-system/postgres-0:${DATA}/$f.upload"
    got=$(PGX sh -c "sha256sum ${DATA}/$f.upload | cut -d' ' -f1")
    [ "$got" = "$want" ] && break
    echo "    upload $try corrupted (${got:0:12}), retrying"
  done
  [ "$got" = "$want" ] || { echo "upload of $f failed"; exit 1; }
  [ -n "$have" ] && PGX sh -c "cp -p ${DATA}/$f ${DATA}/$f.prev"   # rollback copy
  PGX sh -c "mv ${DATA}/$f.upload ${DATA}/$f"
done
if [ "${DRY_RUN}" != 1 ]; then
  for f in bin_ateapi.gz bin_atelet.gz; do
    served=$(PGX sh -c "wget -qO- ${URL}/$f | sha256sum | cut -d' ' -f1")
    [ "$served" = "$(sha256sum "${BIN_DIR}/$f" | cut -d' ' -f1)" ] || { echo "HTTP check failed for $f"; exit 1; }
    echo "    served over HTTP: $f ok"
  done
fi

spec_of() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["spec"],sort_keys=True))'; }
apply_patch() {  # $1=kind/name $2=patch -> prints "changed" or "unchanged"
  local before after
  before=$(K -n ate-system get "$1" -o json | spec_of)
  after=$(K -n ate-system patch "$1" --type=strategic -p "$2" --dry-run=server -o json | spec_of)
  if [ "$before" = "$after" ]; then echo unchanged; return; fi
  [ "${DRY_RUN}" = 1 ] || K -n ate-system patch "$1" --type=strategic -p "$2" >/dev/null
  echo changed
}

# 3. ate-api-server
API_PATCH=$(cat <<EOF
{"spec":{"template":{"spec":{
  "volumes":[{"name":"bin-vol","emptyDir":{}}],
  "initContainers":[{"name":"fetch-bin","image":"alpine:3.19",
    "command":["sh","-c","wget -qO- ${URL}/bin_ateapi.gz | gunzip > /opt/bin/ateapi && chmod +x /opt/bin/ateapi"],
    "volumeMounts":[{"name":"bin-vol","mountPath":"/opt/bin"}]}],
  "containers":[{"name":"ate-api-server","command":["/opt/bin/ateapi"],
    "volumeMounts":[{"name":"bin-vol","mountPath":"/opt/bin"}]}]}}}}
EOF
)
r=$(apply_patch deploy/ate-api-server "$API_PATCH"); echo "    deploy/ate-api-server spec: $r"
if [ "${DRY_RUN}" != 1 ]; then
  if [ "$r" = changed ]; then K -n ate-system rollout status deploy/ate-api-server --timeout=300s
  elif [[ "$CHANGED" == *bin_ateapi.gz* ]]; then K -n ate-system rollout restart deploy/ate-api-server; K -n ate-system rollout status deploy/ate-api-server --timeout=300s; fi
fi

# 4. atelet DaemonSet
ATELET_DS=$(K -n ate-system get ds -o name | awk -F/ '/atelet-/{print $2; exit}')
LET_PATCH=$(cat <<EOF
{"spec":{"template":{"spec":{
  "initContainers":[{"name":"fetch-bin","image":"alpine:3.19",
    "command":["sh","-c","wget -qO- ${URL}/bin_atelet.gz | gunzip > /var/lib/ateom-gvisor/bin_atelet.new && chmod +x /var/lib/ateom-gvisor/bin_atelet.new && mv /var/lib/ateom-gvisor/bin_atelet.new /var/lib/ateom-gvisor/bin_atelet"],
    "volumeMounts":[{"name":"run-ateom","mountPath":"/var/lib/ateom-gvisor"}]}],
  "containers":[{"name":"atelet","command":["/var/lib/ateom-gvisor/bin_atelet"]}]}}}}
EOF
)
r=$(apply_patch "ds/${ATELET_DS}" "$LET_PATCH"); echo "    ds/${ATELET_DS} spec: $r"
if [ "${DRY_RUN}" != 1 ]; then
  if [ "$r" = changed ]; then K -n ate-system rollout status "ds/${ATELET_DS}" --timeout=300s
  elif [[ "$CHANGED" == *bin_atelet.gz* ]]; then K -n ate-system rollout restart "ds/${ATELET_DS}"; K -n ate-system rollout status "ds/${ATELET_DS}" --timeout=300s; fi
fi
say "done"
