#!/usr/bin/env bash
# Launch the 11 18-convo chains as 4 parallel processes.
# Each gets its own ChunkRetriever (no shared CrossEncoder → no MPS thread-safety bug).
#
# Usage (from repo root):
#   SOKRATIC_USE_V2_FLOW=1 SOKRATIC_RETRIEVER=chunks bash scripts/run_eval_parallel.sh
set -u

cd "$(dirname "$0")/.."
PY=.venv/bin/python
LOG_DIR=/tmp/sokratic_eval_chains
rm -rf "$LOG_DIR"
mkdir -p "$LOG_DIR"

# 11 chains split into 4 buckets — biggest chains first to keep wall time tight.
BUCKET_0="eval18_triple1_progressing eval18_solo1_S1 eval18_solo5_S5"
BUCKET_1="eval18_triple2_exploratory eval18_solo2_S2 eval18_solo6_S6"
BUCKET_2="eval18_pair1_strong eval18_pair3_disengaged eval18_solo3_S3"
BUCKET_3="eval18_pair2_moderate eval18_solo4_S4"

run_bucket() {
    local bucket_id=$1
    shift
    local log="$LOG_DIR/bucket_${bucket_id}.log"
    {
        echo "=== bucket $bucket_id starting ==="
        for sid in "$@"; do
            echo "=== bucket $bucket_id chain $sid ==="
            $PY scripts/run_eval_chain.py "$sid"
        done
        echo "=== bucket $bucket_id complete ==="
    } > "$log" 2>&1
}

# Launch each bucket as a true background process. `&` here works because
# we're not capturing the PID via command substitution — we just let bash
# spawn them and we wait on the job table at the end.
run_bucket 0 $BUCKET_0 &
PID0=$!
run_bucket 1 $BUCKET_1 &
PID1=$!
run_bucket 2 $BUCKET_2 &
PID2=$!
run_bucket 3 $BUCKET_3 &
PID3=$!

echo "[launcher] buckets launched: $PID0 $PID1 $PID2 $PID3"
echo "[launcher] tail logs: tail -f $LOG_DIR/bucket_*.log"

wait $PID0 $PID1 $PID2 $PID3
echo
echo "[launcher] all buckets done. summary:"
for i in 0 1 2 3; do
    echo
    echo "--- bucket $i ---"
    grep -E "DONE|chain complete|EXC" "$LOG_DIR/bucket_$i.log" || echo "  (no DONE events)"
done
