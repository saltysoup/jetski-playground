#!/bin/bash
for i in $(seq 1 120); do
  ts=$(date -u +%H:%M:%S)
  line=$(kubectl get pods -n default -l ray.io/cluster --no-headers 2>/dev/null | awk '{printf "%s=%s/%s ", $1, $2, $3}')
  echo "$ts $line"
  r=$(kubectl get pods -n default -l ray.io/cluster --no-headers 2>/dev/null | awk '$3=="Running" && $2=="2/2"' | wc -l)
  [ "$r" = "3" ] && { echo "$ts ALL 3 PODS READY"; break; }
  sleep 30
done
