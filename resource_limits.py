"""Portable resource discovery used for initial scheduler budgets."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess

MIB = 1024 * 1024


def _number(path):
    try:
        value = int(path.read_text().strip())
        return value if 0 <= value < 2**60 else None
    except (OSError, ValueError):
        return None


def _linux_memory():
    try:
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, text = line.partition(':')
            if key in ('MemTotal', 'MemAvailable'):
                values[key] = int(text.split()[0]) * 1024
        return values.get('MemTotal'), values.get('MemAvailable')
    except (OSError, ValueError, IndexError):
        return None, None


def _cgroup_memory():
    roots = [Path('/sys/fs/cgroup'), Path('/sys/fs/cgroup/memory')]
    candidates = set(roots)
    try:
        for line in Path('/proc/self/cgroup').read_text().splitlines():
            _, controllers, relative = line.split(':', 2)
            if controllers and 'memory' not in controllers.split(','):
                continue
            for root in roots:
                path = root / relative.lstrip('/')
                if '..' in path.parts:
                    continue
                while path == root or root in path.parents:
                    candidates.add(path)
                    if path == root:
                        break
                    path = path.parent
    except (OSError, ValueError):
        pass
    limits, available = [], []
    for path in candidates:
        for limit_name, used_name in [('memory.max', 'memory.current'), ('memory.limit_in_bytes', 'memory.usage_in_bytes')]:
            limit = _number(path / limit_name)
            if limit:
                limits.append(limit)
                used = _number(path / used_name)
                if used is not None:
                    available.append(max(0, limit - used))
    return min(limits) if limits else None, min(available) if available else None


def memory_resources():
    total, available = _linux_memory()
    if not total:
        try:
            total = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
        except (ValueError, OSError, AttributeError):
            try:
                total = int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], timeout=2, stderr=subprocess.DEVNULL))
            except (ValueError, OSError, subprocess.SubprocessError):
                total = None
    limit, cgroup_available = _cgroup_memory()
    capacities = [value for value in (total, limit) if value is not None and value > 0]
    capacity = min(capacities) if capacities else None
    free_values = [value for value in (available, cgroup_available) if value is not None]
    free = min(free_values + ([capacity] if capacity is not None else [])) if free_values else None
    return {'memory_total_mb': total // MIB if total else None,
            'memory_capacity_mb': capacity // MIB if capacity else None,
            'memory_available_mb': free // MIB if free is not None else None}


def default_memory_pool_mb(resources=None):
    info = memory_resources() if resources is None else resources
    limits = [info[key] for key in ('memory_capacity_mb', 'memory_available_mb') if info.get(key) is not None]
    if not limits:
        return 8192  # Explicit fallback only when discovery is unavailable.
    base = min(limits)
    budget = max(1, int(base * .8))
    unit = 4096 if base >= 32768 else 1024 if base >= 4096 else 256
    return budget // unit * unit or budget
