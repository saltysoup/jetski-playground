#!/bin/bash
# Restore the two in-container mutations the job depends on, on every Ray pod.
#
# Both live inside the container filesystem, so ANY pod recreation destroys them —
# the head-pod recreation on 19 Aug, the mount-option fix on 20 Aug, and the worker
# pod that comes back after a node replacement in phase B all wipe them. Run this
# before every submission; it is idempotent.
#
#   1. the custom recipe config (never shipped in the image)
#   2. the rank-0-only tokenizer save patch (gcsfuse OSError 116 otherwise)
#
# Secrets are NOT handled here — they are a Kubernetes Secret and survive recreation.
set -euo pipefail

NS=default
SRC=/home/user/reliability-demo/run6
CONFIG=dapo-gemma3-27b-it-2n8g-fsdp2-automodel.yaml
CONFIG_DIR=/opt/nemo-rl/examples/configs/recipes/llm

mapfile -t PODS < <(kubectl get pods -n "$NS" -l ray.io/cluster=ray-cluster-kuberay \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.labels.ray\.io/node-type}{"\n"}{end}')

for entry in "${PODS[@]}"; do
  pod=${entry%% *}; type=${entry##* }
  if [ "$type" = "head" ]; then c=ray-head; else c=ray-worker; fi
  echo "=== $pod ($c)"

  kubectl exec -i -n "$NS" "$pod" -c "$c" -- bash -c "mkdir -p $CONFIG_DIR && cat > $CONFIG_DIR/$CONFIG" < "$SRC/$CONFIG"
  kubectl exec -i -n "$NS" "$pod" -c "$c" -- python3 - < "$SRC/patch-tokenizer-rank0.py"

  # Assert the run6 config specifically: run5's differs only in `checkpointing.enabled`,
  # so grepping for the checkpoint_dir would pass on a stale copy and silently restore
  # checkpointing to a run that is meant to have it off.
  kubectl exec -n "$NS" "$pod" -c "$c" -- bash -c "
    ckpt=\$(awk '/^checkpointing:/{f=1;next} /^[^ ]/{f=0} f&&/^  enabled:/{print \$2}' $CONFIG_DIR/$CONFIG)
    [ \"\$ckpt\" = 'false' ] && echo '  config OK (checkpointing off)' || { echo \"  CONFIG BAD (checkpointing.enabled=\$ckpt)\"; exit 1; }
    [ \$(grep -c 'Rank-0 only' /opt/nemo-rl/nemo_rl/models/automodel/checkpoint.py) -eq 1 ] && echo '  patch OK' || { echo '  PATCH BAD'; exit 1; }
    mountpoint -q /gcs/ckpt && echo '  /gcs/ckpt mounted' || { echo '  MOUNT MISSING'; exit 1; }
  "
done
echo "All pods prepared."
