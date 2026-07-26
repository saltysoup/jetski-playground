#!/bin/bash
REPLICA_COUNT=2

helm install ray-cluster . \
  --set additionalWorkerGroups.worker-grp-0.replicas=$REPLICA_COUNT