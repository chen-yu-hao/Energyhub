"""Reuse the existing PySCF interpreter without installing into it."""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys


def default_data_dir() -> Path:
    """Keep mutable jobs outside the installed package, including read-only wheels."""
    configured = os.environ.get('ENERGYHUB_DATA_DIR')
    if configured:
        return Path(configured).expanduser()
    base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    return base.expanduser() / 'energyhub'


def _executable(value: str) -> str | None:
    path = Path(value).expanduser()
    if not path.is_file():
        found = shutil.which(value)
        if not found:
            return None
        path = Path(found)
    # Do not resolve symlinks: a venv's bin/python must retain its venv prefix.
    return str(path.absolute()) if os.access(path, os.X_OK) else None


def _python_candidates() -> list[str]:
    """Discover local interpreters without machine-specific installation paths."""
    candidates = [sys.executable]
    if os.environ.get('ENERGYHUB_PYTHON'):
        candidates.append(os.environ['ENERGYHUB_PYTHON'])
    candidates.extend(filter(None, (shutil.which('python3'), shutil.which('python'))))
    prefixes = [Path(sys.prefix), Path(sys.base_prefix)]
    if os.environ.get('CONDA_PREFIX'):
        prefixes.append(Path(os.environ['CONDA_PREFIX']))
    conda = os.environ.get('CONDA_EXE') or shutil.which('conda') or shutil.which('mamba')
    if conda:
        prefixes.append(Path(conda).expanduser().absolute().parent.parent)
    environment_roots = set()
    for prefix in prefixes:
        environment_roots.add(prefix / 'envs')
        if prefix.parent.name == 'envs':
            environment_roots.add(prefix.parent)
    for root in sorted(environment_roots):
        # A named environment is a useful hint, but every candidate is probed.
        candidates.append(str(root / 'pyscf' / 'bin' / 'python'))
        candidates.extend(str(path) for path in sorted(root.glob('*/bin/python')))
    return list(dict.fromkeys(path for value in candidates if (path := _executable(value))))


def _probe(executable: str, code: str) -> str | None:
    try:
        result = subprocess.run([executable, '-c', code], capture_output=True, text=True,
                                timeout=10, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _pyscf_version(executable: str) -> tuple[int, ...] | None:
    result = _probe(executable, "import sys, importlib.metadata; assert sys.version_info >= (3, 10); print(importlib.metadata.version('pyscf'))")
    if not result:
        return None
    match = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?', result)
    return tuple(int(part or 0) for part in match.groups()) if match else None


def python_executable() -> str:
    """Respect explicit configuration; otherwise prefer an existing PySCF >=2.14."""
    configured = os.environ.get('ENERGYHUB_PYTHON')
    if configured:
        executable = _executable(configured)
        if executable:
            return executable
        raise RuntimeError('ENERGYHUB_PYTHON is not an executable Python path: ' + configured)
    available = []
    for executable in _python_candidates():
        version = _pyscf_version(executable)
        if version is not None:
            if version >= (2, 14):
                return executable
            available.append((version, executable))
    if available:
        return max(available, key=lambda item: item[0])[1]
    raise RuntimeError('PySCF was not found. Set ENERGYHUB_PYTHON to your existing Python executable, or install dfthub-energyhub[all].')


def api_python_executable() -> str:
    """Used by the source launcher to reuse an existing Flask environment."""
    configured = os.environ.get('ENERGYHUB_API_PYTHON')
    if configured:
        executable = _executable(configured)
        if executable:
            return executable
        raise RuntimeError('ENERGYHUB_API_PYTHON is not executable: ' + configured)
    for executable in _python_candidates():
        if _probe(executable, "import sys, importlib.util; assert sys.version_info >= (3, 10); assert importlib.util.find_spec('flask'); print('ok')") == 'ok':
            return executable
    raise RuntimeError('Flask was not found. Install dfthub-energyhub[api] or set ENERGYHUB_API_PYTHON.')


def worker_environment(threads: int = 1, scratch: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for name in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        env[name] = str(threads)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # Expose only this package, never the API environment's entire site-packages:
    # the external PySCF environment must load its own NumPy/SciPy binary wheels.
    bridge = str(Path(__file__).resolve().parent / '_bootstrap')
    env['PYTHONPATH'] = bridge + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    if scratch is not None:
        env['TMPDIR'] = str(scratch)
        env['PYSCF_TMPDIR'] = str(scratch)
    return env


def stop_process(process: subprocess.Popen, *, group: bool = False) -> None:
    """Reap the leader and kill its process group, including resistant children."""
    grouped = group and os.name == 'posix'
    if not grouped and process.poll() is not None:
        return
    try:
        if grouped:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # A reaped leader does not prove that its numerical workers have exited.
    # The group remains addressable using its original PGID after leader exit.
    try:
        if grouped:
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait(timeout=5)
