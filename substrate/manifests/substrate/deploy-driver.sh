#!/usr/bin/env bash
# deploy-driver.sh — deploy the keynote driver (light demo) and its dashboard into
# pod keynote-demo/keynote-driver, or point a running driver at new TPU-side pods.
#
# Steps:
#   1. kubectl apply keynote-driver.yaml (namespace, RBAC, pod) if kubectl diff shows a
#      difference. The driver runs as a bare Pod with an emptyDir /work: if its node is
#      drained the pod is deleted and nothing re-creates it; this step does.
#   2. Looks up the Envoy gateway's node IP and the pod IPs of vLLM pod-1, pod-2 and
#      the llm-d EPP in the TPU cluster (exactly one Running pod each).
#   3. Builds the driver's args from them and compares them with /work/args.
#   4. Copies dashboard/index.html to /work/static (served from disk: no restart).
#   5. Copies ${BIN_DIR}/keynote_driver to /work/keynote_driver, if BIN_DIR is set.
#   6. If the binary or the args changed: writes /work/args (if it changed) and
#      restarts the driver, then waits until it answers on :8090. The pod's shell
#      loop starts /work/keynote_driver again whenever it exits.
#   7. Prints the driver's state.
# Copies go through `kubectl exec -i` and are checked by sha256 before they replace
# the old file, which is kept as <file>.prev-<sha256 prefix>. The checksum is printed
# by the same exec that writes the file: an exec whose command prints nothing can end
# before its stdin arrives (measured on the demo workstation, implementation.md §6).
#
# Idempotent: on an up-to-date pod it changes nothing and restarts nothing.
#   DRY_RUN=1   report only.
#
# Run it for the first deploy (USER_GUIDE.md Step 10), after the vLLM or EPP pods
# restart (their IPs change), after the keynote-driver pod was deleted, and to ship
# a new dashboard, driver build or driver flags (USER_GUIDE.md §4 and §5).
#
# The Hermes driver (second process in the same pod, port 8092) is not touched:
# busybox `pidof keynote_driver` does not match keynote_driver_hermes. See
# ../../hermes/README.md §4.5.
#
# Usage:
#   CTX_SUB=<substrate kube context> CTX_TPU=<tpu kube context> \
#     [BIN_DIR=<dir with a keynote_driver build>] ./deploy-driver.sh
# Optional: WORKER_NODES (25), WORKER_NODE_TYPE (c4-standard-4), WORKER_NODE_VCPUS (4)
# only change the dashboard's grid and density labels; EXTRA_ARGS adds driver flags
# (pass them on every run: the args are rebuilt each time).
# For testing against a scratch pod: DRIVER_MANIFEST, DRIVER_NS, DRIVER_POD.
set -euo pipefail
CTX_SUB="${CTX_SUB:?set CTX_SUB to the Substrate cluster kube context}"
CTX_TPU="${CTX_TPU:?set CTX_TPU to the TPU cluster kube context}"
BIN_DIR="${BIN_DIR:-}"
DRY_RUN="${DRY_RUN:-0}"
WORKER_NODES="${WORKER_NODES:-25}"
WORKER_NODE_TYPE="${WORKER_NODE_TYPE:-c4-standard-4}"
WORKER_NODE_VCPUS="${WORKER_NODE_VCPUS:-4}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${HERE}/../.." && pwd)"
MANIFEST="${DRIVER_MANIFEST:-${HERE}/keynote-driver.yaml}"
NS="${DRIVER_NS:-keynote-demo}"
POD="${DRIVER_POD:-keynote-driver}"
D() { kubectl --context="${CTX_SUB}" -n "${NS}" "$@"; }
X() { D exec "${POD}" -c driver -- "$@"; }
T() { kubectl --context="${CTX_TPU}" "$@"; }
say() { echo "=== $*"; }
sha_local() { sha256sum "$1" | cut -d' ' -f1; }
sha_pod() { X sh -c "sha256sum '$1' 2>/dev/null | cut -d' ' -f1" || true; }

# copy_verified LOCAL REMOTE MODE [LABEL]: upload to REMOTE.upload, check sha256, keep
# the old REMOTE as REMOTE.prev-<sha8>, then rename into place.
# The remote command must print something after `cat`: an exec whose command prints
# nothing (sh -c 'cat > f') can end before its stdin arrives, leaving f empty.
copy_verified() {
  local src=$1 dst=$2 mode=$3 label=${4:-$(basename "$1")} want got="" old try
  want=$(sha_local "$src")
  for try in 1 2 3; do
    got=$(D exec -i "${POD}" -c driver -- sh -c \
      "mkdir -p '$(dirname "$dst")' && cat > '${dst}.upload' && sha256sum '${dst}.upload' | cut -d' ' -f1" < "$src" || true)
    [ "$got" = "$want" ] && break
    echo "    upload ${try} of ${label} corrupted (${got:0:8}), retrying"
  done
  [ "$got" = "$want" ] || { echo "    upload of ${label} failed" >&2; exit 1; }
  old=$(sha_pod "$dst")
  X sh -c "chmod ${mode} '${dst}.upload' && { [ -z '${old}' ] || cp -p '${dst}' '${dst}.prev-${old:0:8}'; } && mv -f '${dst}.upload' '${dst}'"
  echo "    copied ${label} (${want:0:8})${old:+, previous kept as $(basename "$dst").prev-${old:0:8}}"
}

say "1. Pod ${NS}/${POD}"
# kubectl diff: 0 = same as the cluster, 1 = differs, >1 = could not compare (for
# example the namespace doesn't exist yet, or a change a running Pod can't take).
DIFF_RC=0
DIFF_OUT=$(kubectl --context="${CTX_SUB}" diff -f "${MANIFEST}" 2>&1) || DIFF_RC=$?
if [ "${DIFF_RC}" = 0 ]; then
  echo "    $(basename "${MANIFEST}"): unchanged"
elif [ "${DRY_RUN}" = 1 ]; then
  if [ "${DIFF_RC}" = 1 ]; then
    echo "    [dry-run] $(basename "${MANIFEST}") differs from the cluster: would kubectl apply it"
    printf '%s\n' "${DIFF_OUT}" | awk '/^[-+][^-+]/ && !/generation:/ && n++ < 20 {print "      " $0}'
  else
    echo "    [dry-run] kubectl diff could not compare (first deploy?): would kubectl apply it"
    printf '%s\n' "${DIFF_OUT}" | awk 'NR <= 3 {print "      " $0}'
  fi
else
  kubectl --context="${CTX_SUB}" apply -f "${MANIFEST}" | sed 's/^/    /' || {
    echo "    apply failed. A running Pod can't be changed in place: delete it with" >&2
    echo "    kubectl --context=${CTX_SUB} -n ${NS} delete pod ${POD}   (wipes /work and stops the Hermes driver too)" >&2
    echo "    and run this script again with BIN_DIR set." >&2
    exit 1
  }
fi
if [ "${DRY_RUN}" = 1 ] && ! D get pod "${POD}" >/dev/null 2>&1; then
  echo "    [dry-run] pod ${POD} does not exist yet; a real run creates it, then needs BIN_DIR"
  exit 0
fi
[ "${DRY_RUN}" = 1 ] || D wait --for=condition=Ready "pod/${POD}" --timeout=180s >/dev/null

say "2. TPU cluster addresses"
# one SELECTOR FIELD: FIELD of the single Running pod that matches SELECTOR
one() {
  local out n
  out=$(T get pod -l "$1" --field-selector=status.phase=Running \
    -o jsonpath="{range .items[*]}{$2}{\"\\n\"}{end}" | awk 'NF')
  n=$(printf '%s' "$out" | awk 'NF' | wc -l)
  if [ "$n" -ne 1 ]; then
    echo "    expected 1 Running pod for -l $1 in ${CTX_TPU}, found ${n}; wait for it and re-run" >&2
    exit 1
  fi
  printf '%s' "$out"
}
GATEWAY_IP=$(one app=llmd-envoy-gateway .status.hostIP)
POD1_IP=$(one app=gemma4-12b-torchtpu,llm-d.ai/replica=pod-1 .status.podIP)
POD2_IP=$(one app=gemma4-12b-torchtpu,llm-d.ai/replica=pod-2 .status.podIP)
EPP_IP=$(one inferencepool=gaie-pd-epp .status.podIP)
echo "    gateway ${GATEWAY_IP}:8080 · pod-1 ${POD1_IP}:8000 · pod-2 ${POD2_IP}:8000 · epp ${EPP_IP}:9090"
NOT_READY=$(T get pod -l 'app in (gemma4-12b-torchtpu,llmd-envoy-gateway)' --field-selector=status.phase=Running \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | awk '$2!="True"{print $1}')
[ -z "${NOT_READY}" ] || echo "    note: not Ready yet (still loading?): ${NOT_READY//$'\n'/ }"

say "3. Driver args"
ARGS="-listen=:8090 -static-dir=/work/static -runs-dir=/work/runs -ateapi=api.ate-system.svc:443 -atenet=atenet-router.ate-system.svc:80 -atespace=ate-demo-sandbox -agents=1000 -model=google/gemma-4-12B-it -gateway-url=http://${GATEWAY_IP}:8080/v1/chat/completions -vllm=pod-1=${POD1_IP}:8000,pod-2=${POD2_IP}:8000 -epp=${EPP_IP}:9090 -max-tokens=50 -temperature=1.0 -rest-mode=pause -grpc-conns=32 -suspend-concurrency=200 -nodes=${WORKER_NODES} -node-type=${WORKER_NODE_TYPE} -node-vcpus=${WORKER_NODE_VCPUS}${EXTRA_ARGS:+ ${EXTRA_ARGS}}"
norm() { tr -s ' \t\n' '\n\n\n' | awk 'NF'; }
CUR_ARGS=$(X sh -c 'cat /work/args 2>/dev/null || true')
RESTART=0
ARGS_CHANGED=0
if [ "$(printf '%s' "${CUR_ARGS}" | norm)" = "$(printf '%s' "${ARGS}" | norm)" ]; then
  echo "    /work/args: unchanged"
else
  echo "    /work/args: changing"
  diff <(printf '%s' "${CUR_ARGS}" | norm) <(printf '%s' "${ARGS}" | norm) | awk '/^[<>]/{print "      " $0}' || true
  ARGS_CHANGED=1
  RESTART=1
fi

say "4. Dashboard (dashboard/index.html)"
want=$(sha_local "${REPO_DIR}/dashboard/index.html")
have=$(sha_pod /work/static/index.html)
if [ "$want" = "$have" ]; then
  echo "    unchanged (${want:0:8})"
elif [ "${DRY_RUN}" = 1 ]; then
  echo "    [dry-run] would copy: ${have:0:8} -> ${want:0:8}"
else
  copy_verified "${REPO_DIR}/dashboard/index.html" /work/static/index.html 644
fi

say "5. Driver binary"
have=$(sha_pod /work/keynote_driver)
if [ -z "${BIN_DIR}" ]; then
  if [ -z "$have" ]; then
    echo "    the pod has no driver yet: set BIN_DIR to the directory with your keynote_driver build (USER_GUIDE.md Step 3)" >&2
    [ "${DRY_RUN}" = 1 ] && exit 0 || exit 1
  fi
  echo "    BIN_DIR not set: keeping the pod's binary (${have:0:8})"
else
  want=$(sha_local "${BIN_DIR}/keynote_driver")
  if [ "$want" = "$have" ]; then
    echo "    unchanged (${want:0:8})"
  elif [ "${DRY_RUN}" = 1 ]; then
    echo "    [dry-run] would copy: ${have:0:8} -> ${want:0:8}"
    RESTART=1
  else
    copy_verified "${BIN_DIR}/keynote_driver" /work/keynote_driver 755
    RESTART=1
  fi
fi

say "6. Driver process"
OLD_PID=$(X sh -c 'pidof keynote_driver || true')
if [ "${RESTART}" = 0 ]; then
  echo "    nothing changed: not restarted (pid ${OLD_PID:-none})"
elif [ "${DRY_RUN}" = 1 ]; then
  WHAT="restart the driver"
  [ "${ARGS_CHANGED}" = 0 ] || WHAT="write /work/args and ${WHAT}"
  echo "    [dry-run] would ${WHAT} (pid ${OLD_PID:-none})"
else
  if [ "${ARGS_CHANGED}" = 1 ]; then
    ARGS_FILE=$(mktemp)
    trap 'rm -f "${ARGS_FILE}"' EXIT
    printf '%s\n' "${ARGS}" > "${ARGS_FILE}"
    copy_verified "${ARGS_FILE}" /work/args 644 args
  fi
  [ -z "${OLD_PID}" ] || X sh -c "kill ${OLD_PID}" || true
  NEW_PID=""
  for _ in $(seq 1 30); do
    sleep 1
    NEW_PID=$(X sh -c 'pidof keynote_driver || true')
    if [ -n "${NEW_PID}" ] && [ "${NEW_PID}" != "${OLD_PID}" ] && X wget -qO /dev/null -T 2 http://127.0.0.1:8090/api/state 2>/dev/null; then
      break
    fi
    NEW_PID=""
  done
  if [ -z "${NEW_PID}" ]; then
    echo "    the driver did not come back within 30 s; last log lines:" >&2
    X tail -n 20 /work/driver.log >&2 || true
    exit 1
  fi
  echo "    restarted: pid ${OLD_PID:-none} -> ${NEW_PID}"
  echo "    a kubectl port-forward to this pod can hang after a restart: if the dashboard shows RECONNECTING, restart it"
fi

say "7. Driver state"
X wget -qO- -T 5 http://127.0.0.1:8090/api/state 2>/dev/null | python3 -c '
import collections, json, sys
s = json.load(sys.stdin)
pods = ", ".join("%s %s" % (p.get("name"), "up" if p.get("up") else "DOWN") for p in (s.get("llmd") or {}).get("pods", []))
print("    phase %s · agents %s · vLLM: %s · stage %s" % (s.get("phase"), dict(collections.Counter(s.get("agents", ""))), pods or "?", (s.get("traffic") or {}).get("strategy")))
if s.get("note"):
    print("    note: %s" % s["note"])
' || echo "    api/state not reachable from inside the pod" >&2
say "done"
