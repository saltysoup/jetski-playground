#!/bin/bash
# Poll the checkpoint prefix through the head pod's gcsfuse mount and log every
# change. gcloud auth is expired on the workstation, so the pod is the only way
# to see the bucket right now.
#
# Watches for the thing runs ckptval/ckptval2 never produced: a finalised
# `step_5/` directory, i.e. the tmp_step_5 -> step_5 rename actually landing.
set -u
HEAD=${1:-ray-cluster-kuberay-head-nt7b6}
DIR=/gcs/ckpt/dapo-gemma3-27b-it-2n8g
prev=""
while true; do
  now=$(kubectl exec "$HEAD" -c ray-head -- bash -c "du -s --block-size=1M $DIR 2>/dev/null | cut -f1; ls $DIR 2>/dev/null | tr '\n' ' '" 2>/dev/null)
  mib=$(echo "$now" | head -1)
  entries=$(echo "$now" | tail -1)
  line="$(date -u +%H:%M:%S) | ${mib:-?} MiB | ${entries:-<empty>}"
  if [ "$entries|$mib" != "$prev" ]; then
    echo "$line"
    case "$entries" in
      *step_5*) case "$entries" in *tmp_step_5*) ;; *) echo "$(date -u +%H:%M:%S) | *** FINALISED step_5 PRESENT — rename succeeded ***" ;; esac ;;
    esac
    prev="$entries|$mib"
  fi
  sleep 15
done
