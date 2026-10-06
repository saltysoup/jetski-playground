#!/bin/bash
# Readable view of train.log: drops progress bars, vLLM/Ray worker chatter and
# the metrics-scrape timeouts, keeping setup milestones and step results.
tr '\r' '\n' < /home/user/reliability-demo/train.log \
 | sed 's/\x1b\[[0-9;]*m//g' \
 | grep -vE "examples/s\]?$|it/s\]?$|B/s\]?$|worker/s\]?$|^\s*$" \
 | grep -viE "^\+ (export|[A-Z_]+=)" \
 | grep -vE "^\(|Error fetching metrics from|FutureWarning|UserWarning|warnings\.warn" \
 | grep -vE "image processor of type|Overwriting environment variable" \
 | tail -${1:-30}
