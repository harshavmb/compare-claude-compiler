#!/bin/bash
# benchmark_kernel.sh - Comprehensive kernel compilation benchmark script
# Usage: benchmark_kernel.sh <compiler_type> <cc_path> <kernel_dir> <output_dir>
#
# compiler_type: "gcc" or "ccc"
# cc_path: path to the C compiler binary
# kernel_dir: path to linux-6.9 source
# output_dir: where to store results

set -e

COMPILER_TYPE="${1:?Usage: $0 <gcc|ccc> <cc_path> <kernel_dir> <output_dir>}"
CC_PATH="${2:?Missing cc_path}"
KERNEL_DIR="${3:?Missing kernel_dir}"
OUTPUT_DIR="${4:?Missing output_dir}"
NPROC=$(nproc)

mkdir -p "$OUTPUT_DIR"

echo "============================================"
echo "  Linux Kernel 6.9 Compilation Benchmark"
echo "  Compiler: $COMPILER_TYPE ($CC_PATH)"
echo "  Kernel:   $KERNEL_DIR"
echo "  CPUs:     $NPROC"
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

# Clean kernel build dir
echo "[1/6] Cleaning kernel source..."
cd "$KERNEL_DIR"
make mrproper 2>/dev/null || true

# Configure kernel with defconfig
echo "[2/6] Configuring kernel (defconfig)..."
if [ "$COMPILER_TYPE" = "ccc" ]; then
    make CC="$CC_PATH" HOSTCC="$CC_PATH" defconfig 2>&1 | tee "$OUTPUT_DIR/config.log"
else
    make CC="$CC_PATH" defconfig 2>&1 | tee "$OUTPUT_DIR/config.log"
fi

# Start system monitoring in background
echo "[3/6] Starting resource monitoring..."
MONITOR_PID=""
(
    while true; do
        TIMESTAMP=$(date +%s.%N)
        CPU_USAGE=$(grep 'cpu ' /proc/stat | awk '{total=$2+$3+$4+$5+$6+$7+$8; idle=$5; printf "%.2f", (1-idle/total)*100}')
        MEM_INFO=$(free -k | grep Mem)
        MEM_TOTAL=$(echo "$MEM_INFO" | awk '{print $2}')
        MEM_USED=$(echo "$MEM_INFO" | awk '{print $3}')
        MEM_PCT=$(awk "BEGIN {printf \"%.2f\", ($MEM_USED/$MEM_TOTAL)*100}")
        SWAP_INFO=$(free -k | grep Swap)
        SWAP_USED=$(echo "$SWAP_INFO" | awk '{print $3}')
        LOAD=$(cat /proc/loadavg | awk '{print $1, $2, $3}')
        NPROCS=$(pgrep -c -f "cc1\|ccc\|as\|ld" 2>/dev/null || echo 0)
        IO_STAT=$(cat /proc/diskstats | grep "sda " | awk '{print $6, $10}')  # reads, writes (sectors)
        echo "$TIMESTAMP $CPU_USAGE $MEM_USED $MEM_PCT $SWAP_USED $LOAD $NPROCS $IO_STAT"
        sleep 2
    done
) > "$OUTPUT_DIR/system_metrics.log" &
MONITOR_PID=$!

# Compile the kernel
echo "[4/6] Compiling kernel with $COMPILER_TYPE (j$NPROC)..."

# Capture memory baseline before compilation
FREE_BEFORE=$(free -k | grep Mem | awk '{print $3}')
echo "mem_before_kb: $FREE_BEFORE" >> "$OUTPUT_DIR/system_info.txt"

# Use /usr/bin/time for detailed resource stats
if [ "$COMPILER_TYPE" = "ccc" ]; then
    /usr/bin/time -v make CC="$CC_PATH" HOSTCC="$CC_PATH" -j"$NPROC" vmlinux 2>&1 | tee "$OUTPUT_DIR/compile.log"
else
    /usr/bin/time -v make CC="$CC_PATH" -j"$NPROC" vmlinux 2>&1 | tee "$OUTPUT_DIR/compile.log"
fi
COMPILE_EXIT=$?

# Capture memory after compilation
FREE_AFTER=$(free -k | grep Mem | awk '{print $3}')
echo "mem_after_kb: $FREE_AFTER" >> "$OUTPUT_DIR/system_info.txt"

# Stop monitoring
echo "[5/6] Stopping resource monitor..."
kill $MONITOR_PID 2>/dev/null || true
wait $MONITOR_PID 2>/dev/null || true

# Extract timing and resource data from /usr/bin/time output
echo "[6/6] Collecting results..."

# Parse /usr/bin/time output
grep "Elapsed (wall clock) time" "$OUTPUT_DIR/compile.log" | awk -F: '{print "wall_clock_time: " $NF}' | xargs > "$OUTPUT_DIR/timing.txt"
grep "Maximum resident set size" "$OUTPUT_DIR/compile.log" | awk -F: '{print "max_rss_kb:" $2}' | xargs >> "$OUTPUT_DIR/timing.txt"
grep "Minor (reclaiming a frame) page faults" "$OUTPUT_DIR/compile.log" | awk -F: '{print "minor_page_faults:" $2}' | xargs >> "$OUTPUT_DIR/timing.txt"
grep "Major (requiring I/O) page faults" "$OUTPUT_DIR/compile.log" | awk -F: '{print "major_page_faults:" $2}' | xargs >> "$OUTPUT_DIR/timing.txt"
grep "Voluntary context switches" "$OUTPUT_DIR/compile.log" | awk -F: '{print "voluntary_ctx_switches:" $2}' | xargs >> "$OUTPUT_DIR/timing.txt"
grep "Involuntary context switches" "$OUTPUT_DIR/compile.log" | awk -F: '{print "involuntary_ctx_switches:" $2}' | xargs >> "$OUTPUT_DIR/timing.txt"
grep "Percent of CPU this job got" "$OUTPUT_DIR/compile.log" | awk -F: '{print "cpu_percent:" $2}' | xargs >> "$OUTPUT_DIR/timing.txt"
grep "User time" "$OUTPUT_DIR/compile.log" | awk -F: '{print "user_time_sec:" $2}' | xargs >> "$OUTPUT_DIR/timing.txt"
grep "System time" "$OUTPUT_DIR/compile.log" | awk -F: '{print "system_time_sec:" $2}' | xargs >> "$OUTPUT_DIR/timing.txt"
grep "File system inputs" "$OUTPUT_DIR/compile.log" | awk -F: '{print "fs_inputs:" $2}' | xargs >> "$OUTPUT_DIR/timing.txt"
grep "File system outputs" "$OUTPUT_DIR/compile.log" | awk -F: '{print "fs_outputs:" $2}' | xargs >> "$OUTPUT_DIR/timing.txt"

echo "compile_exit_code: $COMPILE_EXIT" >> "$OUTPUT_DIR/timing.txt"

# Collect binary sizes
if [ -f "$KERNEL_DIR/vmlinux" ]; then
    echo "vmlinux_bytes: $(stat -c%s "$KERNEL_DIR/vmlinux")" >> "$OUTPUT_DIR/timing.txt"
    echo "vmlinux_stripped_bytes: $(strip --strip-debug -o /tmp/vmlinux_stripped "$KERNEL_DIR/vmlinux" && stat -c%s /tmp/vmlinux_stripped)" >> "$OUTPUT_DIR/timing.txt"
    
    # Section sizes
    size "$KERNEL_DIR/vmlinux" > "$OUTPUT_DIR/vmlinux_size.txt" 2>/dev/null || true
    
    # Detailed section info
    objdump -h "$KERNEL_DIR/vmlinux" > "$OUTPUT_DIR/vmlinux_sections.txt" 2>/dev/null || true
fi

# Count object files and their total size
echo "total_object_files: $(find "$KERNEL_DIR" -name '*.o' | wc -l)" >> "$OUTPUT_DIR/timing.txt"
echo "total_object_size_bytes: $(find "$KERNEL_DIR" -name '*.o' -exec du -cb {} + | tail -1 | awk '{print $1}')" >> "$OUTPUT_DIR/timing.txt"

echo ""
echo "============================================"
echo "  Benchmark Complete!"
echo "  Results stored in: $OUTPUT_DIR"
echo "============================================"
echo ""
echo "Key Results:"
cat "$OUTPUT_DIR/timing.txt"
