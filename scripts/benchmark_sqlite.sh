#!/bin/bash
# benchmark_sqlite.sh - SQLite compilation and runtime benchmark (V2 - fair)
# Removes VACUUM (I/O-dominated, not CPU-bound), adds more compute-intensive SQL
# Usage: benchmark_sqlite.sh <compiler_type> <cc_path> <output_dir>

set -e

COMPILER_TYPE="${1:?Usage: $0 <gcc|ccc> <cc_path> <output_dir>}"
CC_PATH="${2:?Missing cc_path}"
OUTPUT_DIR="${3:?Missing output_dir}"
NPROC=$(nproc)
WORKDIR="/tmp/sqlite_bench_v2_${COMPILER_TYPE}"

mkdir -p "$OUTPUT_DIR" "$WORKDIR"
cd "$WORKDIR"

echo "============================================"
echo "  SQLite Benchmark V2 (Fair Comparison)"
echo "  Compiler: $COMPILER_TYPE ($CC_PATH)"
echo "  Date:     $(date -Iseconds)"
echo "============================================"

# Save system info
cat > "$OUTPUT_DIR/system_info.txt" << SYSEOF
hostname: $(hostname)
compiler_type: $COMPILER_TYPE
cc_path: $CC_PATH
cc_version: $($CC_PATH --version 2>&1 | head -1)
kernel: $(uname -r)
cpus: $NPROC
cpu_model: $(grep "model name" /proc/cpuinfo | head -1 | cut -d: -f2 | xargs)
total_ram_kb: $(grep MemTotal /proc/meminfo | awk '{print $2}')
date: $(date -Iseconds)
SYSEOF

# Download SQLite amalgamation if not already present
if [ ! -f sqlite3.c ]; then
    echo "[1/7] Downloading SQLite..."
    wget -q https://www.sqlite.org/2024/sqlite-amalgamation-3460000.zip
    unzip -o sqlite-amalgamation-3460000.zip
    cp sqlite-amalgamation-3460000/sqlite3.c .
    cp sqlite-amalgamation-3460000/sqlite3.h .
    cp sqlite-amalgamation-3460000/shell.c .
    cp sqlite-amalgamation-3460000/sqlite3ext.h .
else
    echo "[1/7] SQLite source already present"
fi

# Start system monitoring
echo "[2/7] Starting resource monitoring..."
(
    while true; do
        TIMESTAMP=$(date +%s.%N)
        MEM_USED=$(free -k | grep Mem | awk '{print $3}')
        CPU=$(grep 'cpu ' /proc/stat | awk '{total=$2+$3+$4+$5+$6+$7+$8; idle=$5; printf "%.2f", (1-idle/total)*100}')
        echo "$TIMESTAMP $CPU $MEM_USED"
        sleep 1
    done
) > "$OUTPUT_DIR/system_metrics.log" &
MONITOR_PID=$!

# === COMPILATION BENCHMARK ===
echo "[3/7] Compiling SQLite with $COMPILER_TYPE..."

# Compile with no optimization
echo "--- Compile: -O0 ---"
/usr/bin/time -v $CC_PATH -O0 -o sqlite3_O0 sqlite3.c shell.c -lpthread -ldl -lm 2>&1 | tee "$OUTPUT_DIR/compile_O0.log"

# Compile with -O2
echo "--- Compile: -O2 ---"
/usr/bin/time -v $CC_PATH -O2 -o sqlite3_O2 sqlite3.c shell.c -lpthread -ldl -lm 2>&1 | tee "$OUTPUT_DIR/compile_O2.log"

# === BINARY SIZE ===
echo "[4/7] Measuring binary sizes..."
cat > "$OUTPUT_DIR/binary_sizes.txt" << SIZES
sqlite3_O0_bytes: $(stat -c%s sqlite3_O0)
sqlite3_O2_bytes: $(stat -c%s sqlite3_O2)
sqlite3_O0_stripped_bytes: $(strip -o sqlite3_O0_stripped sqlite3_O0 && stat -c%s sqlite3_O0_stripped)
sqlite3_O2_stripped_bytes: $(strip -o sqlite3_O2_stripped sqlite3_O2 && stat -c%s sqlite3_O2_stripped)
SIZES
cat "$OUTPUT_DIR/binary_sizes.txt"

# === RUNTIME SPEED BENCHMARK ===
# Designed to be CPU-bound, no VACUUM (VACUUM is I/O-dominated page reorganization)
echo "[5/7] Running speed benchmark..."
cat > /tmp/sqlite_speed_test_v2.sql << 'SQLEOF'
.timer on

-- === PHASE 1: Table creation and bulk insert ===
CREATE TABLE test1(a INTEGER PRIMARY KEY, b TEXT, c REAL, d INTEGER);
BEGIN;
WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM cnt WHERE x<100000)
INSERT INTO test1 SELECT x, 'text_value_' || x, x * 1.5, x % 1000 FROM cnt;
COMMIT;

-- === PHASE 2: Aggregation queries (CPU-intensive) ===
SELECT COUNT(*), SUM(c), AVG(c), MIN(c), MAX(c) FROM test1;
SELECT d, COUNT(*), AVG(c) FROM test1 GROUP BY d ORDER BY COUNT(*) DESC LIMIT 20;
SELECT d, SUM(c), AVG(a) FROM test1 GROUP BY d HAVING SUM(c) > 10000;

-- === PHASE 3: Sorting (CPU-intensive) ===
SELECT a, b, c FROM test1 ORDER BY c DESC LIMIT 100;
SELECT a, b, c FROM test1 ORDER BY b LIMIT 100;
SELECT a, b FROM test1 ORDER BY c ASC, a DESC LIMIT 100;

-- === PHASE 4: Index creation and indexed queries ===
CREATE INDEX idx_test1_b ON test1(b);
CREATE INDEX idx_test1_c ON test1(c);
CREATE INDEX idx_test1_d ON test1(d);

SELECT COUNT(*) FROM test1 WHERE b LIKE 'text_value_5%';
SELECT * FROM test1 WHERE c BETWEEN 1000.0 AND 2000.0 LIMIT 10;
SELECT * FROM test1 WHERE d = 500;

-- === PHASE 5: Join tests (CPU-intensive) ===
CREATE TABLE test2 AS SELECT a, 'copy_' || b as b2, c * 2 as c2 FROM test1 WHERE a % 10 = 0;
SELECT COUNT(*) FROM test1 INNER JOIN test2 ON test1.a = test2.a;
SELECT t1.a, t1.b, t2.b2 FROM test1 t1 JOIN test2 t2 ON t1.a = t2.a WHERE t1.c > 50000 LIMIT 20;

-- === PHASE 6: Subquery tests ===
SELECT COUNT(*) FROM test1 WHERE a IN (SELECT a FROM test2);
SELECT * FROM test1 WHERE c > (SELECT AVG(c2) FROM test2) LIMIT 10;
SELECT a, b, c FROM test1 WHERE a NOT IN (SELECT a FROM test2) AND d < 100 LIMIT 10;

-- === PHASE 7: Update and Delete (write performance) ===
BEGIN;
UPDATE test1 SET c = c * 2 WHERE a % 3 = 0;
COMMIT;

BEGIN;
UPDATE test1 SET b = 'updated_' || b WHERE d < 50;
COMMIT;

BEGIN;
DELETE FROM test1 WHERE a % 7 = 0;
COMMIT;

SELECT COUNT(*) FROM test1;

-- === PHASE 8: Aggregate computation ===
SELECT d % 100 as bucket, COUNT(*), SUM(c), AVG(c), MIN(a), MAX(a)
FROM test1 GROUP BY d % 100 ORDER BY SUM(c) DESC LIMIT 20;

-- === PHASE 9: Re-insert and more computation ===
CREATE TABLE test3(id INTEGER PRIMARY KEY, val TEXT, num REAL);
BEGIN;
WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM cnt WHERE x<50000)
INSERT INTO test3 SELECT x, hex(randomblob(8)), (x * 7 % 1000) * 0.01 FROM cnt;
COMMIT;

SELECT t1.a, t3.val FROM test1 t1 JOIN test3 t3 ON (t1.a % 50000 + 1) = t3.id LIMIT 100;
SELECT num, COUNT(*) FROM test3 GROUP BY CAST(num * 10 AS INTEGER) ORDER BY COUNT(*) DESC LIMIT 20;

-- === PHASE 10: Cleanup ===
DROP TABLE test2;
DROP TABLE test3;
DROP TABLE test1;

.quit
SQLEOF

# Run speed test with O0 binary — let it complete naturally
echo "--- Speed test: O0 ---"
echo "Start time: $(date -Iseconds)"
rm -f /tmp/bench_v2_o0.db
/usr/bin/time -v ./sqlite3_O0 /tmp/bench_v2_o0.db < /tmp/sqlite_speed_test_v2.sql 2>&1 | tee "$OUTPUT_DIR/speed_O0.log"
echo "End time: $(date -Iseconds)"

# Run speed test with O2 binary — let it complete naturally
echo "--- Speed test: O2 ---"
echo "Start time: $(date -Iseconds)"
rm -f /tmp/bench_v2_o2.db
/usr/bin/time -v ./sqlite3_O2 /tmp/bench_v2_o2.db < /tmp/sqlite_speed_test_v2.sql 2>&1 | tee "$OUTPUT_DIR/speed_O2.log"
echo "End time: $(date -Iseconds)"

# === MEMORY USAGE ===
echo "[6/7] Measuring peak memory during large operation..."
cat > /tmp/sqlite_mem_test_v2.sql << 'MEMEOF'
CREATE TABLE memtest(a INTEGER PRIMARY KEY, b TEXT, c REAL);
BEGIN;
WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM cnt WHERE x<500000)
INSERT INTO memtest SELECT x, 'data_' || x, x * 0.001 FROM cnt;
COMMIT;
SELECT COUNT(*) FROM memtest;
CREATE INDEX idx_memtest ON memtest(b);
SELECT AVG(c) FROM memtest;
.quit
MEMEOF

rm -f /tmp/memtest_v2_o2.db
./sqlite3_O2 /tmp/memtest_v2_o2.db < /tmp/sqlite_mem_test_v2.sql &
SQLITE_PID=$!
PEAK_RSS=0
while kill -0 $SQLITE_PID 2>/dev/null; do
    RSS=$(cat /proc/$SQLITE_PID/status 2>/dev/null | grep VmRSS | awk '{print $2}' || echo 0)
    if [ "$RSS" -gt "$PEAK_RSS" ] 2>/dev/null; then
        PEAK_RSS=$RSS
    fi
    sleep 0.1
done
wait $SQLITE_PID 2>/dev/null || true
echo "peak_rss_kb_runtime_O2: $PEAK_RSS" >> "$OUTPUT_DIR/binary_sizes.txt"
echo "Peak RSS during runtime (O2): ${PEAK_RSS} KB"

# === CRASH / SEGFAULT TEST ===
echo "[7/7] Running crash/segfault tests..."
CRASH_COUNT=0
TOTAL_TESTS=0

# Test 1: NULL operations
cat > /tmp/sqlite_crash1.sql << 'EOF'
CREATE TABLE crash1(a, b, c);
INSERT INTO crash1 VALUES(NULL, NULL, NULL);
INSERT INTO crash1 VALUES(1, 'test', 3.14);
SELECT * FROM crash1 WHERE a IS NULL;
SELECT COALESCE(a, 0) FROM crash1;
SELECT typeof(a), typeof(b) FROM crash1;
.quit
EOF
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if ! ./sqlite3_O2 :memory: < /tmp/sqlite_crash1.sql > /dev/null 2>&1; then
    CRASH_COUNT=$((CRASH_COUNT + 1))
    echo "CRASH: NULL operations test"
fi

# Test 2: Large data
cat > /tmp/sqlite_crash2.sql << 'EOF'
CREATE TABLE crash2(data BLOB);
INSERT INTO crash2 VALUES(randomblob(1000000));
SELECT length(data) FROM crash2;
.quit
EOF
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if ! ./sqlite3_O2 :memory: < /tmp/sqlite_crash2.sql > /dev/null 2>&1; then
    CRASH_COUNT=$((CRASH_COUNT + 1))
    echo "CRASH: Large data test"
fi

# Test 3: Recursive query
cat > /tmp/sqlite_crash3.sql << 'EOF'
WITH RECURSIVE fib(n, a, b) AS (
    VALUES(0, 0, 1)
    UNION ALL
    SELECT n+1, b, a+b FROM fib WHERE n < 50
)
SELECT n, a FROM fib;
.quit
EOF
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if ! ./sqlite3_O2 :memory: < /tmp/sqlite_crash3.sql > /dev/null 2>&1; then
    CRASH_COUNT=$((CRASH_COUNT + 1))
    echo "CRASH: Recursive query test"
fi

# Test 4: Complex expressions
cat > /tmp/sqlite_crash4.sql << 'EOF'
SELECT 1/0;
SELECT CAST('not_a_number' AS INTEGER);
SELECT unicode('');
SELECT zeroblob(0);
SELECT quote(NULL);
SELECT hex(randomblob(32));
.quit
EOF
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if ! ./sqlite3_O2 :memory: < /tmp/sqlite_crash4.sql > /dev/null 2>&1; then
    CRASH_COUNT=$((CRASH_COUNT + 1))
    echo "CRASH: Complex expressions test"
fi

# Test 5: Concurrent-like operations
cat > /tmp/sqlite_crash5.sql << 'EOF'
CREATE TABLE concurrent(id INTEGER PRIMARY KEY, val TEXT);
BEGIN;
INSERT INTO concurrent SELECT value, 'data_' || value FROM generate_series(1,10000);
COMMIT;
BEGIN;
UPDATE concurrent SET val = val || '_updated' WHERE id % 2 = 0;
DELETE FROM concurrent WHERE id % 3 = 0;
INSERT INTO concurrent SELECT id + 10000, val FROM concurrent WHERE id < 1000;
COMMIT;
SELECT COUNT(*) FROM concurrent;
.quit
EOF
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if ! ./sqlite3_O2 :memory: < /tmp/sqlite_crash5.sql > /dev/null 2>&1; then
    CRASH_COUNT=$((CRASH_COUNT + 1))
    echo "CRASH: Concurrent-like operations test"
fi

echo "crash_tests_total: $TOTAL_TESTS" >> "$OUTPUT_DIR/binary_sizes.txt"
echo "crash_tests_failed: $CRASH_COUNT" >> "$OUTPUT_DIR/binary_sizes.txt"

# Stop monitoring
kill $MONITOR_PID 2>/dev/null || true
wait $MONITOR_PID 2>/dev/null || true

# Parse timing results
for OPT in O0 O2; do
    for TEST in compile speed; do
        LOG="$OUTPUT_DIR/${TEST}_${OPT}.log"
        if [ -f "$LOG" ]; then
            WALL=$(grep "Elapsed (wall clock)" "$LOG" | awk -F'): ' '{print $2}')
            RSS=$(grep "Maximum resident" "$LOG" | awk -F': ' '{print $2}')
            USR=$(grep "User time" "$LOG" | awk -F': ' '{print $2}')
            SYS=$(grep "System time" "$LOG" | awk -F': ' '{print $2}')
            echo "${TEST}_${OPT}_wall: $WALL" >> "$OUTPUT_DIR/summary.txt"
            echo "${TEST}_${OPT}_max_rss_kb: $RSS" >> "$OUTPUT_DIR/summary.txt"
            echo "${TEST}_${OPT}_user_time: $USR" >> "$OUTPUT_DIR/summary.txt"
            echo "${TEST}_${OPT}_sys_time: $SYS" >> "$OUTPUT_DIR/summary.txt"
        fi
    done
done

echo ""
echo "============================================"
echo "  SQLite Benchmark V2 Complete!"
echo "  Results in: $OUTPUT_DIR"
echo "============================================"
cat "$OUTPUT_DIR/summary.txt" 2>/dev/null || true
cat "$OUTPUT_DIR/binary_sizes.txt"
