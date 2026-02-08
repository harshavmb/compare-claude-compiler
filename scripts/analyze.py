#!/usr/bin/env python3
"""
Compiler Comparison Analysis & Visualization (V2)
Compares GCC vs Claude's C Compiler (CCC) across 5 key metrics:
1. Compilation time
2. Binary size
3. Runtime speed
4. Memory usage
5. Crash/segfault probability

Reads results from:
  results/kernel_gcc/      - GCC kernel build (original, successful)
  results/kernel_ccc_v2/   - CCC kernel build (v2, gcc_m16 feature)
  results/sqlite_gcc_v2/   - GCC SQLite benchmark (v2, no VACUUM)
  results/sqlite_ccc_v2/   - CCC SQLite benchmark (v2, no VACUUM)
"""

import os
import re
import json
import sys

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("WARNING: matplotlib not installed.")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(BASE_DIR, 'results')
OUTPUT_DIR = os.path.join(BASE_DIR, 'graphs')
os.makedirs(OUTPUT_DIR, exist_ok=True)


def parse_time_string(time_str):
    """Parse time strings like '1:13:11' (h:mm:ss) or '0:06.96' (m:ss.ms) to seconds."""
    if not time_str:
        return 0
    time_str = time_str.strip()
    parts = time_str.split(':')
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    else:
        return float(time_str)


def parse_kv_file(filepath):
    """Parse a key: value file into a dict."""
    data = {}
    if not os.path.exists(filepath):
        return data
    with open(filepath, errors='replace') as f:
        for line in f:
            line = line.strip()
            if ':' in line:
                key, _, val = line.partition(':')
                data[key.strip()] = val.strip()
    return data


def parse_time_output(filepath):
    """Parse /usr/bin/time -v output from a log file."""
    data = {}
    if not os.path.exists(filepath):
        return data
    with open(filepath, errors='replace') as f:
        content = f.read()

    patterns = {
        'wall_clock': r'Elapsed \(wall clock\) time.*?:\s*([\d:]+\.?\d*)',
        'max_rss_kb': r'Maximum resident set size.*?:\s*(\d+)',
        'user_time': r'User time \(seconds\):\s*([\d.]+)',
        'system_time': r'System time \(seconds\):\s*([\d.]+)',
        'cpu_percent': r'Percent of CPU.*?:\s*(\d+)',
        'voluntary_ctx': r'Voluntary context switches:\s*(\d+)',
        'involuntary_ctx': r'Involuntary context switches:\s*(\d+)',
        'minor_faults': r'Minor.*?page faults:\s*(\d+)',
        'major_faults': r'Major.*?page faults:\s*(\d+)',
        'exit_status': r'Exit status:\s*(\d+)',
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, content)
        if match:
            data[key] = match.group(1).strip()

    return data


def parse_system_metrics(filepath):
    """Parse system_metrics.log into (timestamps, cpu_or_load, mem_mb).

    Handles two formats:
      GCC kernel: 11 fields split across 2 lines per sample:
        Line 1: TIMESTAMP CPU% MEM_USED MEM_PCT SWAP_USED LOAD1 LOAD5 LOAD15 NPROCS
        Line 2: IO_READ IO_WRITE
      CCC kernel: 3 fields per line:
        TIMESTAMP LOAD_AVG MEM_USED

    Returns (timestamps_in_minutes, cpu_or_load_values, mem_in_mb).
    Also returns a 'metric_type' attribute on the returned lists: 'cpu_percent' or 'load_avg'.
    """
    timestamps, values, mem_used = [], [], []
    metric_type = 'cpu_percent'
    if not os.path.exists(filepath):
        return timestamps, values, mem_used

    with open(filepath) as f:
        lines = f.readlines()

    # Detect format: if first field of line 0 is a large timestamp and line 1
    # starts with a small number, it's the 2-line GCC format.
    is_multiline = False
    if len(lines) >= 2:
        try:
            first_field_0 = float(lines[0].strip().split()[0])
            first_field_1 = float(lines[1].strip().split()[0])
            # Timestamps are epoch seconds (~1.7 billion). Continuation lines start with 0 or small IO values.
            if first_field_0 > 1_000_000_000 and first_field_1 < 1_000_000_000:
                is_multiline = True
        except (ValueError, IndexError):
            pass

    if is_multiline:
        # GCC format: take every other line (the ones with timestamps)
        metric_type = 'cpu_percent'
        for line in lines:
            parts = line.strip().split()
            if len(parts) < 3:
                continue
            try:
                ts = float(parts[0])
                if ts < 1_000_000_000:
                    continue  # Skip continuation lines (IO_READ IO_WRITE)
                timestamps.append(ts)
                values.append(float(parts[1]))       # CPU%
                mem_used.append(float(parts[2]) / 1024)  # KB -> MB
            except (ValueError, IndexError):
                continue
    else:
        # CCC format: 3 columns, but col 1 is load average, not CPU%
        for line in lines:
            parts = line.strip().split()
            if len(parts) >= 3:
                try:
                    ts = float(parts[0])
                    if ts < 1_000_000_000:
                        continue
                    timestamps.append(ts)
                    values.append(float(parts[1]))       # load average
                    mem_used.append(float(parts[2]) / 1024)  # KB -> MB
                except (ValueError, IndexError):
                    continue
        # Detect if values are load averages (typically 0-100 on a 6-core system)
        # vs CPU% (typically 0-800 for 8-core)
        if values and max(values) < 100:
            metric_type = 'load_avg'

    if timestamps:
        t0 = timestamps[0]
        timestamps = [(t - t0) / 60.0 for t in timestamps]

    # Attach metric type as an attribute
    timestamps = list(timestamps)
    values = list(values)
    mem_used = list(mem_used)
    # Store metric type in a way the caller can access
    return timestamps, values, mem_used, metric_type


def load_kernel_results():
    """Load kernel benchmark results from both original and v2 directories."""
    results = {}

    # GCC: use original results (successful build)
    gcc_dir = os.path.join(RESULTS_DIR, 'kernel_gcc')
    if os.path.isdir(gcc_dir):
        r = {
            'system_info': parse_kv_file(os.path.join(gcc_dir, 'system_info.txt')),
            'timing': parse_kv_file(os.path.join(gcc_dir, 'timing.txt')),
            'compile_log': parse_time_output(os.path.join(gcc_dir, 'compile.log')),
        }
        if 'wall_clock' in r['compile_log']:
            r['wall_clock_seconds'] = parse_time_string(r['compile_log']['wall_clock'])
        r['build_succeeded'] = True
        r['metrics_time'], r['metrics_cpu'], r['metrics_mem'], r['metrics_type'] = parse_system_metrics(
            os.path.join(gcc_dir, 'system_metrics.log'))

        # Count CC lines from compile.log (like we do for CCC)
        compile_log_path = os.path.join(gcc_dir, 'compile.log')
        if os.path.exists(compile_log_path):
            with open(compile_log_path, errors='replace') as f:
                content = f.read()
            cc_lines = len(re.findall(r'^\s+CC\s+', content, re.MULTILINE))
            r['cc_files_compiled'] = cc_lines
        # Fallback to timing.txt total_object_files
        if r.get('cc_files_compiled', 0) == 0:
            r['cc_files_compiled'] = int(r.get('timing', {}).get('total_object_files', '0'))

        results['gcc'] = r

    # CCC: use v2 results (gcc_m16 feature, all .o compiled, link failed)
    ccc_dir = os.path.join(RESULTS_DIR, 'kernel_ccc_v2')
    if os.path.isdir(ccc_dir):
        r = {
            'system_info': parse_kv_file(os.path.join(ccc_dir, 'system_info.txt')),
            'build_status': parse_kv_file(os.path.join(ccc_dir, 'build_status.txt')),
            'compile_log': parse_time_output(os.path.join(ccc_dir, 'compile.log')),
        }
        if 'wall_clock' in r['compile_log']:
            r['wall_clock_seconds'] = parse_time_string(r['compile_log']['wall_clock'])

        # Count object files and check for errors
        compile_log_path = os.path.join(ccc_dir, 'compile.log')
        if os.path.exists(compile_log_path):
            with open(compile_log_path, errors='replace') as f:
                content = f.read()
            # Count CC lines (successful compilations)
            cc_lines = len(re.findall(r'^\s+CC\s+', content, re.MULTILINE))
            r['cc_files_compiled'] = cc_lines

            # Count linker errors
            ld_errors = len(re.findall(r'undefined reference', content))
            r['linker_errors'] = ld_errors

            # Build failed at link stage
            exit_match = re.search(r'Exit status:\s*(\d+)', content)
            r['build_succeeded'] = exit_match and exit_match.group(1) == '0'
            r['build_failed_at'] = 'link' if ld_errors > 0 else ('compile' if not r['build_succeeded'] else None)

        r['metrics_time'], r['metrics_cpu'], r['metrics_mem'], r['metrics_type'] = parse_system_metrics(
            os.path.join(ccc_dir, 'system_metrics.log'))
        results['ccc'] = r

    return results


def load_sqlite_results():
    """Load SQLite benchmark results from v2 directories."""
    results = {}

    for compiler, dirname in [('gcc', 'sqlite_gcc_v2'), ('ccc', 'sqlite_ccc_v2')]:
        d = os.path.join(RESULTS_DIR, dirname)
        if not os.path.isdir(d):
            continue

        r = {
            'system_info': parse_kv_file(os.path.join(d, 'system_info.txt')),
            'binary_sizes': parse_kv_file(os.path.join(d, 'binary_sizes.txt')),
            'summary': parse_kv_file(os.path.join(d, 'summary.txt')),
        }

        # Parse compile and speed logs
        for opt in ['O0', 'O2']:
            for test in ['compile', 'speed']:
                log = os.path.join(d, f'{test}_{opt}.log')
                r[f'{test}_{opt}'] = parse_time_output(log)

        # Fallback from summary.txt
        summary = r['summary']
        for opt in ['O0', 'O2']:
            for test in ['compile', 'speed']:
                key_prefix = f'{test}_{opt}'
                entry = r.get(f'{test}_{opt}', {})
                if key_prefix + '_wall' in summary and 'wall_clock' not in entry:
                    entry['wall_clock'] = summary[key_prefix + '_wall']
                if key_prefix + '_max_rss_kb' in summary and 'max_rss_kb' not in entry:
                    entry['max_rss_kb'] = summary[key_prefix + '_max_rss_kb']
                r[f'{test}_{opt}'] = entry

        results[compiler] = r

    return results


def plot_kernel_comparison(results):
    """Generate kernel compilation comparison charts."""
    if not HAS_MATPLOTLIB:
        return

    compilers = [c for c in ['gcc', 'ccc'] if c in results]
    if len(compilers) < 2:
        print("  Need both GCC and CCC kernel results")
        return

    colors = {'gcc': '#2196F3', 'ccc': '#FF5722'}
    labels = {'gcc': 'GCC 14.2.0', 'ccc': "Claude's C Compiler"}

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle('Linux Kernel 6.9 Compilation: GCC vs Claude\'s C Compiler\n'
                 '(CCC built with gcc_m16 feature — all C files compiled, link failed)',
                 fontsize=14, fontweight='bold')

    # 1. Wall clock time
    ax = axes[0, 0]
    times = [results[c].get('wall_clock_seconds', 0) for c in compilers]
    bars = ax.bar([labels[c] for c in compilers], [t / 60 for t in times], color=[colors[c] for c in compilers])
    ax.set_ylabel('Time (minutes)')
    ax.set_title('Total Build Time (Wall Clock)')
    for bar, t in zip(bars, times):
        ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.5,
                f'{t/60:.1f} min', ha='center', va='bottom', fontweight='bold')
    if results.get('ccc', {}).get('build_failed_at') == 'link':
        ax.text(0.5, -0.12, '⚠ CCC: all C files compiled successfully, failed at link stage',
                transform=ax.transAxes, ha='center', fontsize=8, color='#E65100', style='italic')

    # 2. Peak memory
    ax = axes[0, 1]
    rss_vals = []
    for c in compilers:
        rss = int(results[c]['compile_log'].get('max_rss_kb', '0')) / 1024
        rss_vals.append(rss)
    bars = ax.bar([labels[c] for c in compilers], rss_vals, color=[colors[c] for c in compilers])
    ax.set_ylabel('Peak RSS (MB)')
    ax.set_title('Peak Memory Usage')
    for bar, r in zip(bars, rss_vals):
        if r >= 1024:
            lbl = f'{r/1024:.1f} GB'
        else:
            lbl = f'{r:.0f} MB'
        ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 10,
                lbl, ha='center', va='bottom', fontweight='bold')

    # 3. User CPU time (total across all jobs)
    ax = axes[1, 0]
    user_times = [float(results[c]['compile_log'].get('user_time', '0')) / 60 for c in compilers]
    bars = ax.bar([labels[c] for c in compilers], user_times, color=[colors[c] for c in compilers])
    ax.set_ylabel('User CPU Time (minutes)')
    ax.set_title('Total CPU Time (User)')
    for bar, t in zip(bars, user_times):
        ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.5,
                f'{t:.0f} min', ha='center', va='bottom', fontweight='bold')
    ax.text(0.5, -0.08, 'GCC completed full pipeline (compile → assemble → link → vmlinux)',
            transform=ax.transAxes, ha='center', fontsize=7.5, color='#1565C0', style='italic')
    ax.text(0.5, -0.15, 'CCC failed at link stage — lower CPU time ≠ better',
            transform=ax.transAxes, ha='center', fontsize=7.5, color='#E65100', style='italic')

    # 4. C files compiled + linker errors
    ax = axes[1, 1]
    gcc_cc = results['gcc'].get('cc_files_compiled', 0)
    ccc_cc = results['ccc'].get('cc_files_compiled', 0)
    # If we don't have cc_files_compiled, use object file count
    if gcc_cc == 0:
        gcc_cc = int(results['gcc'].get('timing', {}).get('total_object_files', '0'))
    ld_errors = results['ccc'].get('linker_errors', 0)

    x = np.arange(2)
    bars1 = ax.bar(x, [gcc_cc, ccc_cc], 0.5, color=[colors['gcc'], colors['ccc']])
    ax.set_ylabel('Count')
    ax.set_title('C Files Compiled (CC lines)')
    ax.set_xticks(x)
    ax.set_xticklabels([labels['gcc'], labels['ccc']])
    for bar, n in zip(bars1, [gcc_cc, ccc_cc]):
        ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 20,
                f'{n}', ha='center', va='bottom', fontweight='bold')
    if ld_errors > 0:
        ax.text(0.5, -0.12, f'CCC: 0 compiler errors, {ld_errors} linker errors (relocation issues)',
                transform=ax.transAxes, ha='center', fontsize=8, color='#E65100', style='italic')

    plt.tight_layout()
    outpath = os.path.join(OUTPUT_DIR, 'kernel_comparison.png')
    plt.savefig(outpath, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {outpath}")

    # System metrics over time
    fig, axes = plt.subplots(2, 1, figsize=(14, 8))
    fig.suptitle('System Resource Usage During Kernel Compilation\n'
                 '(GCC completed full build incl. assembly & linking → vmlinux; CCC failed at link stage)',
                 fontsize=13, fontweight='bold')

    # Determine if we have mixed metric types (CPU% for GCC, load_avg for CCC)
    gcc_type = results.get('gcc', {}).get('metrics_type', 'cpu_percent')
    ccc_type = results.get('ccc', {}).get('metrics_type', 'cpu_percent')

    for c in compilers:
        t = results[c].get('metrics_time', [])
        cpu = results[c].get('metrics_cpu', [])
        mem = results[c].get('metrics_mem', [])
        mtype = results[c].get('metrics_type', 'cpu_percent')

        suffix = ' (CPU %)' if mtype == 'cpu_percent' else ' (Load Avg)'
        if t and cpu:
            axes[0].plot(t, cpu, label=f'{labels[c]}{suffix}', color=colors[c], alpha=0.7, linewidth=0.8)            
        if t and mem:
            axes[1].plot(t, mem, label=labels[c], color=colors[c], alpha=0.7, linewidth=0.8)

    # Set appropriate y-axis label based on metric types
    if gcc_type == ccc_type:
        ylabel = 'CPU Usage (%)' if gcc_type == 'cpu_percent' else 'Load Average'
    else:
        ylabel = 'CPU % (GCC) / Load Average (CCC)'
    axes[0].set_ylabel(ylabel)
    axes[0].set_title('CPU / Load During Build')     
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)
    axes[1].set_ylabel('Memory (MB)')
    axes[1].set_xlabel('Time (minutes)')
    axes[1].set_title('Memory Usage')
    axes[1].text(0.5, -0.10,
                '* CCC never built the binary, used GCC assembler and linker',
                transform=ax.transAxes, ha='left', fontsize=7.5, color='#B71C1C', style='italic')   
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)    

    plt.tight_layout()
    outpath = os.path.join(OUTPUT_DIR, 'kernel_system_metrics.png')
    plt.savefig(outpath, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {outpath}")


def plot_sqlite_comparison(results):
    """Generate SQLite benchmark comparison charts."""
    if not HAS_MATPLOTLIB:
        return

    compilers = [c for c in ['gcc', 'ccc'] if c in results]
    if len(compilers) < 2:
        print("  Need both GCC and CCC SQLite results")
        return

    colors = {'gcc': '#2196F3', 'ccc': '#FF5722'}
    labels = {'gcc': 'GCC 14.2.0', 'ccc': "Claude's C Compiler"}

    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    fig.suptitle('SQLite 3.46 Benchmark: GCC vs Claude\'s C Compiler (V2 - Fair Comparison)',
                 fontsize=14, fontweight='bold')

    width = 0.35

    # 1. Compilation time
    ax = axes[0, 0]
    x = np.arange(2)
    for i, c in enumerate(compilers):
        vals = []
        for opt in ['O0', 'O2']:
            wc = results[c].get(f'compile_{opt}', {}).get('wall_clock', '0')
            vals.append(parse_time_string(wc))
        bars = ax.bar(x + (i - 0.5) * width, vals, width, label=labels[c], color=colors[c])
        for bar, v in zip(bars, vals):
            lbl = f'{v:.0f}s' if v < 120 else f'{v/60:.1f}m'
            ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 2,
                    lbl, ha='center', va='bottom', fontsize=8, fontweight='bold')
    ax.set_ylabel('Time (seconds)')
    ax.set_title('Compilation Time')
    ax.set_xticks(x)
    ax.set_xticklabels(['-O0', '-O2'])
    ax.text(0.5, -0.10,
                '* CCC ignores -O2 flag — output is byte-identical to -O0 (no real optimization)',
                transform=ax.transAxes, ha='center', fontsize=7.5, color='#B71C1C', style='italic')
    ax.legend(fontsize=8)

    # 2. Binary size
    ax = axes[0, 1]
    for i, c in enumerate(compilers):
        vals = []
        for opt in ['O0', 'O2']:
            s = int(results[c].get('binary_sizes', {}).get(f'sqlite3_{opt}_bytes', '0'))
            vals.append(s / (1024 * 1024))
        bars = ax.bar(x + (i - 0.5) * width, vals, width, label=labels[c], color=colors[c])
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.03,
                    f'{v:.1f}MB', ha='center', va='bottom', fontsize=8, fontweight='bold')
    ax.set_ylabel('Size (MB)')
    ax.set_title('Binary Size')
    ax.set_xticks(x)
    ax.set_xticklabels(['-O0', '-O2'])
    ax.text(0.5, -0.10,
                '* CCC ignores -O2 flag — output is byte-identical to -O0 (no real optimization)',
                transform=ax.transAxes, ha='center', fontsize=7.5, color='#B71C1C', style='italic')
    ax.legend(fontsize=8)

    # 3. Runtime speed (log scale)
    ax = axes[0, 2]
    has_runtime_data = False
    for i, c in enumerate(compilers):
        vals = []
        for opt in ['O0', 'O2']:
            wc = results[c].get(f'speed_{opt}', {}).get('wall_clock', '0')
            v = parse_time_string(wc)
            vals.append(v if v > 0 else 0.001)
        if any(v > 0.01 for v in vals):
            has_runtime_data = True
        bars = ax.bar(x + (i - 0.5) * width, vals, width, label=labels[c], color=colors[c])
        for bar, v in zip(bars, vals):
            if v > 0.01:
                if v >= 3600:
                    lbl = f'{v/3600:.1f}h'
                elif v >= 60:
                    lbl = f'{v/60:.0f}m'
                else:
                    lbl = f'{v:.1f}s'
                ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() * 1.15,
                        lbl, ha='center', va='bottom', fontsize=8, fontweight='bold')
    ax.set_ylabel('Time (seconds, log scale)')
    ax.set_title('Runtime Speed')
    ax.set_xticks(x)
    ax.set_xticklabels(['-O0', '-O2'])
    if has_runtime_data:
        ax.set_yscale('log')
    ax.legend(fontsize=8)

    # 4. Compiler memory usage
    ax = axes[1, 0]
    for i, c in enumerate(compilers):
        vals = []
        for opt in ['O0', 'O2']:
            r = int(results[c].get(f'compile_{opt}', {}).get('max_rss_kb', '0'))
            vals.append(r / 1024)
        bars = ax.bar(x + (i - 0.5) * width, vals, width, label=labels[c], color=colors[c])
        for bar, v in zip(bars, vals):
            lbl = f'{v/1024:.1f}GB' if v >= 1024 else f'{v:.0f}MB'
            ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 20,
                    lbl, ha='center', va='bottom', fontsize=8, fontweight='bold')
    ax.set_ylabel('Peak RSS (MB)')
    ax.set_title('Compiler Memory Usage')
    ax.set_xticks(x)
    ax.set_xticklabels(['-O0', '-O2'])
    ax.legend(fontsize=8)

    # 5. Crash test results
    ax = axes[1, 1]
    crash_data = []
    for c in compilers:
        total = int(results[c].get('binary_sizes', {}).get('crash_tests_total', '0'))
        failed = int(results[c].get('binary_sizes', {}).get('crash_tests_failed', '0'))
        crash_data.append((total, total - failed, failed))
    xp = np.arange(len(compilers))
    ax.bar(xp, [d[1] for d in crash_data], 0.5, label='Passed', color='#4CAF50')
    ax.bar(xp, [d[2] for d in crash_data], 0.5, bottom=[d[1] for d in crash_data],
           label='Failed', color='#F44336')
    ax.set_ylabel('Tests')
    ax.set_title('Crash/Segfault Tests')
    ax.set_xticks(xp)
    ax.set_xticklabels([labels[c] for c in compilers])
    ax.legend()
    for i, d in enumerate(crash_data):
        ax.text(i, d[0] + 0.1, f'{d[1]}/{d[0]} pass', ha='center', va='bottom',
                fontsize=10, fontweight='bold')

    # 6. CCC/GCC ratio chart
    ax = axes[1, 2]
    metric_labels = []
    ratios = []
    ratio_colors = []

    for opt in ['O0', 'O2']:
        gcc_t = parse_time_string(results['gcc'].get(f'compile_{opt}', {}).get('wall_clock', '0'))
        ccc_t = parse_time_string(results['ccc'].get(f'compile_{opt}', {}).get('wall_clock', '0'))
        if gcc_t > 0 and ccc_t > 0:
            r = ccc_t / gcc_t
            metric_labels.append(f'Compile -{opt}')
            ratios.append(r)
            ratio_colors.append('#FF5722' if r > 1 else '#4CAF50')

    for opt in ['O0', 'O2']:
        gcc_t = parse_time_string(results['gcc'].get(f'speed_{opt}', {}).get('wall_clock', '0'))
        ccc_t = parse_time_string(results['ccc'].get(f'speed_{opt}', {}).get('wall_clock', '0'))
        if gcc_t > 0 and ccc_t > 0:
            r = ccc_t / gcc_t
            metric_labels.append(f'Runtime -{opt}')
            ratios.append(r)
            ratio_colors.append('#FF5722' if r > 1 else '#4CAF50')

    gcc_s = int(results['gcc'].get('binary_sizes', {}).get('sqlite3_O2_bytes', '0'))
    ccc_s = int(results['ccc'].get('binary_sizes', {}).get('sqlite3_O2_bytes', '0'))
    if gcc_s > 0 and ccc_s > 0:
        r = ccc_s / gcc_s
        metric_labels.append('Binary Size')
        ratios.append(r)
        ratio_colors.append('#FF5722' if r > 1 else '#4CAF50')

    gcc_m = int(results['gcc'].get('compile_O0', {}).get('max_rss_kb', '0'))
    ccc_m = int(results['ccc'].get('compile_O0', {}).get('max_rss_kb', '0'))
    if gcc_m > 0 and ccc_m > 0:
        r = ccc_m / gcc_m
        metric_labels.append('Compile Memory')
        ratios.append(r)
        ratio_colors.append('#FF5722' if r > 1 else '#4CAF50')

    if metric_labels:
        y = np.arange(len(metric_labels))
        bars = ax.barh(y, ratios, color=ratio_colors, height=0.6)
        ax.axvline(x=1, color='black', linestyle='--', linewidth=1, alpha=0.5)
        ax.set_yticks(y)
        # Mark Compile -O2 label with asterisk to flag the misleading ratio
        display_labels = []
        for lbl in metric_labels:
            if lbl == 'Compile -O2':
                display_labels.append('Compile -O2 *')
            else:
                display_labels.append(lbl)
        ax.set_yticklabels(display_labels)
        #ax.set_xlabel('Ratio (CCC / GCC)')
        ax.set_title('CCC vs GCC Ratio\n(>1 = CCC worse)')
        if max(ratios) / max(min(ratios), 0.01) > 10:
            ax.set_xscale('log')
        for bar, r in zip(bars, ratios):
            ax.text(bar.get_width() * 1.05, bar.get_y() + bar.get_height()/2.,
                    f'{r:.1f}x', ha='left', va='center', fontsize=9, fontweight='bold')
        ax.text(0.5, -0.10,
                '* CCC ignores -O2 flag — output is byte-identical to -O0 (no real optimization)',
                transform=ax.transAxes, ha='center', fontsize=7.5, color='#B71C1C', style='italic')

    plt.tight_layout()
    outpath = os.path.join(OUTPUT_DIR, 'sqlite_comparison.png')
    plt.savefig(outpath, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {outpath}")

    # Per-query comparison chart
    plot_per_query_comparison()


def plot_per_query_comparison():
    """Generate per-query runtime comparison chart from speed_O0 logs."""
    if not HAS_MATPLOTLIB:
        return

    query_labels = [
        'INSERT 100K', 'SELECT count', 'GROUP BY', 'SELECT agg',
        'ORDER BY c', 'ORDER BY b', 'ORDER BY c,a',
        'CREATE idx_b', 'CREATE idx_c', 'CREATE idx_d',
        'LIKE scan', 'BETWEEN', 'WHERE d=',
        'CREATE test2', 'JOIN ON', 'JOIN WHERE',
        'IN (sub)', 'WHERE > AVG', 'NOT IN (sub)',
        'UPDATE %3', 'UPDATE d<50', 'DELETE %7', 'COUNT(*)',
        'GROUP BY %100',
        'INSERT test3', 'JOIN test3', 'GROUP BY cast',
        'DROP test2', 'DROP test3', 'DROP test1',
    ]

    tsv_path = os.path.join(RESULTS_DIR, 'per_query_O0.tsv')
    if not os.path.exists(tsv_path):
        # Try to generate from logs
        gcc_log = os.path.join(RESULTS_DIR, 'sqlite_gcc_v2', 'speed_O0.log')
        ccc_log = os.path.join(RESULTS_DIR, 'sqlite_ccc_v2', 'speed_O0.log')
        if not os.path.exists(gcc_log) or not os.path.exists(ccc_log):
            return
        gcc_times = []
        ccc_times = []
        for path, times in [(gcc_log, gcc_times), (ccc_log, ccc_times)]:
            with open(path) as f:
                for line in f:
                    if 'Run Time:' in line:
                        m = re.search(r'real\s+([\d.]+)', line)
                        if m:
                            times.append(float(m.group(1)))
    else:
        gcc_times = []
        ccc_times = []
        with open(tsv_path) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2:
                    gcc_times.append(float(parts[0]))
                    ccc_times.append(float(parts[1]))

    n = min(len(gcc_times), len(ccc_times), len(query_labels))
    if n == 0:
        return

    gcc_times = gcc_times[:n]
    ccc_times = ccc_times[:n]
    labels = query_labels[:n]

    # Calculate ratios, handle zero
    ratios = []
    for g, c in zip(gcc_times, ccc_times):
        if g > 0.001 and c > 0.001:
            ratios.append(c / g)
        elif c > 0.001:
            ratios.append(c / 0.001)
        else:
            ratios.append(1.0)

    fig, axes = plt.subplots(2, 1, figsize=(16, 14))
    fig.suptitle('SQLite Per-Query Performance: GCC O0 vs CCC\n'
                 '(42 SQL operations from benchmark_sqlite.sh)',
                 fontsize=14, fontweight='bold')

    # Top chart: absolute times (log scale)
    ax = axes[0]
    x = np.arange(n)
    width = 0.35
    bars1 = ax.bar(x - width/2, gcc_times, width, label='GCC -O0', color='#2196F3', alpha=0.8)
    bars2 = ax.bar(x + width/2, ccc_times, width, label='CCC', color='#FF5722', alpha=0.8)
    ax.set_ylabel('Time (seconds, log scale)')
    ax.set_title('Absolute Runtime per Query')
    ax.set_yscale('log')
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=65, ha='right', fontsize=7)
    ax.legend()
    ax.grid(True, alpha=0.3, axis='y')
    ax.set_ylim(bottom=0.0005)

    # Bottom chart: CCC/GCC ratio
    ax = axes[1]
    colors = ['#F44336' if r > 100 else '#FF9800' if r > 10 else '#FFC107' if r > 2 else '#4CAF50' for r in ratios]
    bars = ax.bar(x, ratios, color=colors)
    ax.axhline(y=1, color='black', linestyle='--', linewidth=1, alpha=0.5)
    ax.axhline(y=10, color='orange', linestyle=':', linewidth=1, alpha=0.4)
    ax.axhline(y=100, color='red', linestyle=':', linewidth=1, alpha=0.4)
    ax.set_ylabel('Slowdown Ratio (CCC / GCC), log scale')
    ax.set_title('CCC Slowdown Factor per Query (higher = worse)')
    ax.set_yscale('log')
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=65, ha='right', fontsize=7)
    ax.grid(True, alpha=0.3, axis='y')

    # Label the extreme outliers
    for i, r in enumerate(ratios):
        if r > 50:
            ax.text(i, r * 1.3, f'{r:.0f}x', ha='center', va='bottom',
                    fontsize=7, fontweight='bold', color='#B71C1C')

    plt.tight_layout()
    outpath = os.path.join(OUTPUT_DIR, 'sqlite_per_query.png')
    plt.savefig(outpath, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {outpath}")


def generate_summary(kernel_results, sqlite_results):
    """Generate comprehensive summary as JSON and text."""
    summary = {'kernel': {}, 'sqlite': {}}

    # Kernel
    for c in ['gcc', 'ccc']:
        if c not in kernel_results:
            continue
        r = kernel_results[c]
        cl = r.get('compile_log', {})
        summary['kernel'][c] = {
            'wall_clock_seconds': r.get('wall_clock_seconds', 0),
            'wall_clock_human': f"{r.get('wall_clock_seconds', 0)/60:.1f} min",
            'user_time_seconds': float(cl.get('user_time', 0)),
            'system_time_seconds': float(cl.get('system_time', 0)),
            'peak_rss_mb': int(cl.get('max_rss_kb', 0)) / 1024,
            'cpu_percent': cl.get('cpu_percent', 'N/A'),
            'build_succeeded': r.get('build_succeeded', False),
            'build_failed_at': r.get('build_failed_at', None),
            'cc_files_compiled': r.get('cc_files_compiled', 0),
            'linker_errors': r.get('linker_errors', 0),
        }

    # SQLite
    for c in ['gcc', 'ccc']:
        if c not in sqlite_results:
            continue
        r = sqlite_results[c]
        summary['sqlite'][c] = {}
        for opt in ['O0', 'O2']:
            compile_data = r.get(f'compile_{opt}', {})
            speed_data = r.get(f'speed_{opt}', {})
            summary['sqlite'][c][opt] = {
                'compile_time_seconds': parse_time_string(compile_data.get('wall_clock', '0')),
                'compile_peak_rss_mb': int(compile_data.get('max_rss_kb', 0)) / 1024,
                'runtime_seconds': parse_time_string(speed_data.get('wall_clock', '0')),
                'runtime_peak_rss_mb': int(speed_data.get('max_rss_kb', 0)) / 1024,
                'binary_size_bytes': int(r.get('binary_sizes', {}).get(f'sqlite3_{opt}_bytes', 0)),
            }
        summary['sqlite'][c]['crash_tests'] = {
            'total': int(r.get('binary_sizes', {}).get('crash_tests_total', 0)),
            'failed': int(r.get('binary_sizes', {}).get('crash_tests_failed', 0)),
        }

    # Save JSON
    json_path = os.path.join(OUTPUT_DIR, 'summary.json')
    with open(json_path, 'w') as f:
        json.dump(summary, f, indent=2)
    print(f"  Saved: {json_path}")

    # Print text summary
    print("\n" + "=" * 80)
    print("  COMPILER COMPARISON: GCC 14.2 vs Claude's C Compiler (CCC)")
    print("=" * 80)

    if 'gcc' in summary['kernel'] and 'ccc' in summary['kernel']:
        kg = summary['kernel']['gcc']
        kc = summary['kernel']['ccc']
        print(f"\n{'LINUX KERNEL 6.9':^80}")
        print("-" * 80)
        print(f"{'Metric':<35} {'GCC':>20} {'CCC':>20}")
        print("-" * 80)
        print(f"{'Build Result':<35} {'SUCCESS':>20} {'LINK FAILED':>20}")
        print(f"{'Compilation Time':<35} {kg['wall_clock_human']:>20} {kc['wall_clock_human']:>20}")
        print(f"{'User CPU Time':<35} {kg['user_time_seconds']/60:>18.1f}m {kc['user_time_seconds']/60:>18.1f}m")
        print(f"{'Peak RSS':<35} {kg['peak_rss_mb']:>18.0f}MB {kc['peak_rss_mb']:>18.0f}MB")
        print(f"{'C Files Compiled':<35} {kg.get('cc_files_compiled', 'N/A'):>20} {kc.get('cc_files_compiled', 'N/A'):>20}")
        print(f"{'Compiler Errors':<35} {'0':>20} {'0':>20}")
        print(f"{'Linker Errors':<35} {'0':>20} {kc.get('linker_errors', 0):>20}")
        print(f"\n  Note: CCC compiled ALL C files successfully (0 errors).")
        print(f"  Link failed due to incorrect relocations in __jump_table and __ksymtab.")

    if 'gcc' in summary['sqlite'] and 'ccc' in summary['sqlite']:
        sg = summary['sqlite']['gcc']
        sc = summary['sqlite']['ccc']
        print(f"\n{'SQLITE 3.46':^80}")
        print("-" * 80)
        print(f"{'Metric':<35} {'GCC':>20} {'CCC':>20}")
        print("-" * 80)
        for opt in ['O0', 'O2']:
            if opt in sg and opt in sc:
                print(f"\n  -{opt}:")
                g, c = sg[opt], sc[opt]
                ct_ratio = c['compile_time_seconds'] / g['compile_time_seconds'] if g['compile_time_seconds'] > 0 else 0
                print(f"{'    Compile Time':<35} {g['compile_time_seconds']:>18.1f}s {c['compile_time_seconds']:>18.1f}s  ({ct_ratio:.1f}x)")
                bs_ratio = c['binary_size_bytes'] / g['binary_size_bytes'] if g['binary_size_bytes'] > 0 else 0
                print(f"{'    Binary Size':<35} {g['binary_size_bytes']/1024:>17.0f}KB {c['binary_size_bytes']/1024:>17.0f}KB  ({bs_ratio:.1f}x)")
                if c['runtime_seconds'] > 0:
                    rt_ratio = c['runtime_seconds'] / g['runtime_seconds'] if g['runtime_seconds'] > 0 else 0
                    print(f"{'    Runtime':<35} {g['runtime_seconds']:>18.1f}s {c['runtime_seconds']:>18.1f}s  ({rt_ratio:.0f}x)")
                else:
                    print(f"{'    Runtime':<35} {g['runtime_seconds']:>18.1f}s {'(running...)':>20}")
                cm_ratio = c['compile_peak_rss_mb'] / g['compile_peak_rss_mb'] if g['compile_peak_rss_mb'] > 0 else 0
                print(f"{'    Compile Memory':<35} {g['compile_peak_rss_mb']:>17.0f}MB {c['compile_peak_rss_mb']:>17.0f}MB  ({cm_ratio:.1f}x)")

        gt = sg.get('crash_tests', {})
        ct = sc.get('crash_tests', {})
        gp = gt.get('total', 0) - gt.get('failed', 0)
        cp = ct.get('total', 0) - ct.get('failed', 0)
        print(f"\n{'  Crash Tests':<35} {gp}/{gt.get('total',0):>14} pass {cp}/{ct.get('total',0):>14} pass")

        # Key insight about CCC optimization
        if sc.get('O0') and sc.get('O2'):
            o0_size = sc['O0']['binary_size_bytes']
            o2_size = sc['O2']['binary_size_bytes']
            if o0_size > 0 and abs(o0_size - o2_size) < 100:
                print(f"\n  Note: CCC -O0 and -O2 produce identical binaries ({o0_size/1024:.0f} KB)")
                print(f"  CCC runs the same optimization pipeline at all -O levels.")

    print("\n" + "=" * 80)
    return summary


def main():
    print("=" * 60)
    print("  CCC vs GCC Benchmark Analysis (V2)")
    print("=" * 60)

    print("\nLoading results...")
    kernel_results = load_kernel_results()
    sqlite_results = load_sqlite_results()

    print(f"  Kernel: {list(kernel_results.keys())}")
    print(f"  SQLite: {list(sqlite_results.keys())}")

    print("\nGenerating charts...")
    plot_kernel_comparison(kernel_results)
    plot_sqlite_comparison(sqlite_results)

    print("\nGenerating summary...")
    generate_summary(kernel_results, sqlite_results)

    print(f"\nOutputs: {OUTPUT_DIR}/")


if __name__ == '__main__':
    main()
