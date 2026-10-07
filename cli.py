"""Portable command-line interface for the web application and calculations."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from . import __version__
from .runtime import default_data_dir, python_executable, worker_environment


def _positive(value):
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError('must be a positive integer') from error
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def _parser():
    parser = argparse.ArgumentParser(prog='energyhub', description='Compute DFThub reference energies with PySCF, or serve the Energyhub web application.')
    parser.add_argument('--version', action='version', version=f'Energyhub {__version__}')
    parser.add_argument('--python', help='existing PySCF Python executable (also ENERGYHUB_PYTHON)', default=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve', help='start the frontend and HTTP API')
    serve.add_argument('--resources', choices=['auto', 'manual'], default=os.environ.get('ENERGYHUB_RESOURCE_MODE'), help='automatic live resource detection or fixed manual limits')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=_positive, default=2022)
    serve.add_argument('--pool-size', type=_positive, default=os.environ.get('ENERGYHUB_POOL_SIZE'), help='global concurrent molecule limit')
    serve.add_argument('--thread-pool-size', type=_positive, default=os.environ.get('ENERGYHUB_THREAD_POOL_SIZE'), help='global CPU thread budget')
    serve.add_argument('--memory-pool-mb', type=_positive, default=os.environ.get('ENERGYHUB_MEMORY_MB'), help='global reserved PySCF memory budget in MiB')
    serve.add_argument('--data-dir', type=Path, default=os.environ.get('ENERGYHUB_DATA_DIR'), help='persistent jobs directory (default: XDG user data directory)')
    run = commands.add_parser('run', help='calculate a complete .ref file from an archive')
    run.add_argument('tgz', type=Path, help='DFThub .tgz/.tar.gz archive')
    run.add_argument('--ref', type=Path, help='reference template; omit to use the archive template or molecular energies')
    run.add_argument('--output', '-o', type=Path, help='destination .ref file')
    run.add_argument('--method', choices=['CCSD', 'CCSD(T)', 'CCSDT', 'CCSDT(Q)'], default='CCSD(T)')
    run.add_argument('--basis', choices=['3zeta', '4zeta', '5zeta', 'CBS'], default='3zeta')
    run.add_argument('--basis-family', choices=['cc', 'aug'], default='cc')
    run.add_argument('--cbs-pair', choices=['34', '45'], default='34')
    run.add_argument('--pool-size', type=_positive, default=1, help='concurrent molecules')
    run.add_argument('--task-threads', type=_positive, default=1, help='threads per molecule')
    run.add_argument('--memory-mb', type=_positive, default=4096, help='PySCF memory per molecule in MiB')
    run.add_argument('--memory-pool-mb', type=_positive, help='total memory reservation in MiB')
    doctor = commands.add_parser('doctor', help='inspect the selected Python, PySCF methods and data location')
    doctor.add_argument('--json', action='store_true', help='machine-readable output')
    for command in (serve, run, doctor):
        command.add_argument('--python', help='existing PySCF Python executable', default=argparse.SUPPRESS)
    return parser


def _serve(args):
    try:
        from .api import create_app
    except ModuleNotFoundError as error:
        if error.name == 'flask':
            raise RuntimeError('Flask is required for the web application. Install dfthub-energyhub[api].') from error
        raise
    app = create_app(data_dir=args.data_dir, pool_size=args.pool_size,
                     memory_pool_mb=args.memory_pool_mb, thread_pool_size=args.thread_pool_size, resource_mode=args.resources)
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        signal.signal(signal.SIGTERM, previous)
        app.extensions['energyhub_manager'].close()
    return 0


def _run(args):
    from .core import compute_reference

    def progress(value):
        if value.get('status') == 'running':
            print(f"Energyhub: {value.get('completed', 0)}/{value.get('total', '?')} molecules", file=sys.stderr, flush=True)

    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        report = compute_reference(args.tgz, args.ref, args.output, method=args.method,
                                   basis=args.basis, basis_family=args.basis_family, cbs_pair=args.cbs_pair,
                                   pool_size=args.pool_size, task_threads=args.task_threads,
                                   memory_mb=args.memory_mb, memory_pool_mb=args.memory_pool_mb,
                                   progress=progress)
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _doctor(args):
    executable = python_executable()
    code = 'import json; from Energyhub.capabilities import get_capabilities; print(json.dumps(get_capabilities()))'
    result = subprocess.run([executable, '-c', code], env=worker_environment(), capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Cannot inspect the selected Python: ' + result.stderr[-2000:])
    details = json.loads(result.stdout)
    details.update({'energyhub_version': __version__, 'python': executable, 'data_dir': str(default_data_dir()), 'platform': sys.platform})
    if args.json:
        print(json.dumps(details, ensure_ascii=False, indent=2))
    else:
        print(f'Energyhub {__version__}\nPython: {executable}\nPySCF: {details["pyscf_version"] or "not installed"}\nData: {details["data_dir"]}')
        for method in details['methods']:
            status = f'closed shell: {method["closed_shell"]}; open shell: {method["open_shell"]}'
            print(f'  {method["name"]}: {status}' + (f' — {method["reason"]}' if method.get('reason') else ''))
    return 0 if any(item['available'] for item in details['methods']) else 1


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if os.name != 'posix':
        parser.error('Energyhub currently supports Linux and macOS; on Windows use WSL2.')
    if getattr(args, 'python', None):
        os.environ['ENERGYHUB_PYTHON'] = args.python
    try:
        return {'serve': _serve, 'run': _run, 'doctor': _doctor}[args.command](args)
    except KeyboardInterrupt:
        print('Energyhub: cancelled.', file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'Energyhub: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
