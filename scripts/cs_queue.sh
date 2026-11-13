#!/bin/bash
# Run a list of tasks x variants, N in parallel. Usage: cs_queue.sh PREFIX "task1 task2" "A B" [PARALLEL]
P=$1; TASKS=$2; VARS=${3:-"A B"}; N=${4:-2}
D=$(dirname "$0")
for t in $TASKS; do for v in $VARS; do echo "$t $v"; done; done \
  | xargs -P "$N" -L1 sh -c 'bash '"$D"'/cs_run.sh '"$P"'-$0 $1 $0'
echo "$(date -u +%H:%M) queue $P done" >> $HOME/ple/logs/summary.txt
