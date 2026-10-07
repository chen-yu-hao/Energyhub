"""Single-service queue with global molecule slots and memory reservations."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Literal
import uuid

from .core import METHODS, parse_reference
from .runtime import default_data_dir, python_executable, stop_process, worker_environment
from .storage import write_json
from .resource_limits import memory_resources, default_memory_pool_mb

BASIS = ('3zeta', '4zeta', '5zeta', 'CBS')
TERMINAL_STATES = {'completed', 'failed', 'cancelled'}
JobState = Literal['queued', 'running', 'cancelling', 'completed', 'failed', 'cancelled']
RESOURCE_REFRESH_SECONDS = 2.0
WORKER_OPTIONS = ('method', 'basis', 'basis_family', 'cbs_pair', 'pool_size', 'task_threads', 'memory_mb', 'memory_pool_mb')


def available_cpus():
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def display_name(value, name, default):
    value = default if value is None else str(value).strip()
    if not value or len(value) > 160 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f'{name} must contain 1–160 characters without control characters')
    return value


def upload_filename(upload, default):
    value = getattr(upload, 'filename', None)
    if value is None and isinstance(upload, (str, os.PathLike)):
        value = str(upload)
    # Browser uploads may contain Windows paths even on a POSIX server.
    value = str(value or default).replace('\\', '/').rsplit('/', 1)[-1]
    return ''.join(char for char in value if ord(char) >= 32 and ord(char) != 127)[:255] or default


def positive_int(value, name):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)) or int(value) < 1:
        raise ValueError(f'{name} must be a positive integer')
    return int(value)


@dataclass
class Job:
    task_id: str
    directory: Path
    options: dict
    name: str = ''
    dataset: str = ''
    archive_filename: str = 'input.tgz'
    reference_filename: str = 'input.ref'
    state: str = 'queued'
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: Any = None
    error: str | None = None
    report: dict | None = None
    process: Any = None
    future: Any = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def output_path(self):
        return self.directory / 'result.ref'

    @property
    def result_filename(self):
        stem = re.sub(r'[/\\:*?"<>|]', '_', self.dataset or self.task_id).strip('. ')
        return f'{stem or self.task_id}.ref'

    def as_dict(self):
        result = {'task_id': self.task_id, 'state': self.state, 'status': self.state,
                  **self.options, 'created_at': self.created_at, 'started_at': self.started_at,
                  'finished_at': self.finished_at, 'progress': self.progress,
                  'error': self.error, 'report': self.report, 'name': self.name or self.task_id,
                  'dataset': self.dataset or self.task_id, 'archive_filename': self.archive_filename,
                  'reference_filename': self.reference_filename}
        if self.state == 'completed':
            result['download_url'] = f'/api/energyhub/jobs/{self.task_id}/result'
            result['report_url'] = f'/api/energyhub/jobs/{self.task_id}/report'
            result['result_filename'] = self.result_filename
        return result


class EnergyJobManager:
    """Own one queue. pool_size is the global concurrent-molecule limit.

    memory_pool_mb reserves the sum of active tasks' molecular memory budgets.
    PySCF's max_memory is a working-memory hint, not a hard operating-system RSS
    limit; native-library overhead should be allowed for when sizing the pool.
    """
    def __init__(self, data_dir=None, pool_size=None, memory_pool_mb=None, *,
                 thread_pool_size=None, resource_mode=None, worker_module='Energyhub.worker'):
        self.data_dir = Path(data_dir or default_data_dir()).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        saved = {}
        if (self.data_dir / 'config.json').exists():
            try:
                saved = json.loads((self.data_dir / 'config.json').read_text(encoding='utf-8'))
                if not isinstance(saved, dict):
                    raise ValueError('expected a JSON object')
            except (OSError, ValueError) as error:
                raise ValueError(f'Cannot read saved resource configuration: {error}') from error
        explicit_limits = any(value is not None for value in (pool_size, memory_pool_mb, thread_pool_size))
        saved_mode = saved.get('resource_mode', 'manual' if any(key in saved for key in ('pool_size', 'memory_pool_mb', 'thread_pool_size')) else 'auto')
        self.resource_mode = resource_mode if resource_mode is not None else ('manual' if explicit_limits else saved_mode)
        if self.resource_mode not in ('auto', 'manual'):
            raise ValueError('resource_mode must be auto or manual')
        if resource_mode == 'auto' and explicit_limits:
            raise ValueError('Automatic mode detects resource limits; omit fixed pool limits')
        self._resource_snapshot = memory_resources()
        self._detected_cpus = available_cpus()
        self._recommended_memory = default_memory_pool_mb(self._resource_snapshot)
        self._resources_checked_at = time.monotonic()
        self._resources_wall_time = time.time()
        self._retired_executors = []
        if self.resource_mode == 'auto':
            self.pool_size = self.thread_pool_size = self._detected_cpus
            self.memory_pool_mb = self._recommended_memory
        else:
            self.pool_size = positive_int(saved.get('pool_size', self._detected_cpus) if pool_size is None else pool_size, 'pool_size')
            self.memory_pool_mb = positive_int(saved.get('memory_pool_mb', self._recommended_memory) if memory_pool_mb is None else memory_pool_mb, 'memory_pool_mb')
            self.thread_pool_size = positive_int(saved.get('thread_pool_size', self._detected_cpus) if thread_pool_size is None else thread_pool_size, 'thread_pool_size')
        if self.thread_pool_size > self._detected_cpus:
            raise ValueError('thread_pool_size exceeds available logical CPUs')
        self._directory_lock = (self.data_dir / '.service.lock').open('a+')
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(self._directory_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._directory_lock.close()
                raise RuntimeError('This data directory already has an Energyhub scheduler; use one server process')
        self._worker_module = worker_module
        self.python = python_executable()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._memory_used = self._slots_used = self._threads_used = 0
        self._closed = False
        self._capabilities = None
        self._capabilities_checked_at = time.monotonic()
        self._jobs = {}
        self._executor = ThreadPoolExecutor(max_workers=self.pool_size, thread_name_prefix='energyhub')
        self._executor_capacity = self.pool_size
        for metadata in self.data_dir.glob('*/task.json'):
            job = self._load_metadata(metadata.parent.name)
            if job:
                if job.state not in TERMINAL_STATES:
                    job.state, job.error, job.finished_at = 'failed', 'Service stopped before the task completed', time.time()
                    self._persist(job)
                self._jobs[job.task_id] = job

    def capabilities(self, *, refresh=False):
        with self._lock:
            if refresh or self._capabilities is None or time.monotonic() - self._capabilities_checked_at > 60:
                code = 'import json; from Energyhub.capabilities import get_capabilities; print(json.dumps(get_capabilities()))'
                try:
                    completed = subprocess.run([self.python, '-c', code], env=worker_environment(), capture_output=True, text=True, timeout=30)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError('PySCF environment inspection timed out after 30 seconds') from error
                if completed.returncode:
                    raise RuntimeError('Cannot inspect configured PySCF environment: ' + completed.stderr[-2000:])
                try:
                    self._capabilities = json.loads(completed.stdout)
                except ValueError as error:
                    raise RuntimeError('PySCF environment returned an invalid capability report') from error
                self._capabilities_checked_at = time.monotonic()
            return self._capabilities

    def _resize_executor_locked(self):
        """Move pending supervisors without touching running molecular processes."""
        if self._executor_capacity == self.pool_size:
            return
        previous = self._executor
        self._executor = ThreadPoolExecutor(max_workers=self.pool_size, thread_name_prefix='energyhub')
        self._executor_capacity = self.pool_size
        for job in self._jobs.values():
            if job.state == 'queued' and job.future is not None and job.future.cancel():
                job.future = self._executor.submit(self._execute, job)
        previous.shutdown(wait=False)
        self._retired_executors.append(previous)

    def _refresh_resources_locked(self, *, force=False):
        if self._closed:
            return
        if force or time.monotonic() - self._resources_checked_at >= RESOURCE_REFRESH_SECONDS:
            self._resource_snapshot = memory_resources()
            self._detected_cpus = available_cpus()
            self._recommended_memory = default_memory_pool_mb(self._resource_snapshot)
            self._resources_checked_at = time.monotonic()
            self._resources_wall_time = time.time()
        if self.resource_mode == 'auto':
            old = self.pool_size, self.thread_pool_size, self.memory_pool_mb
            # A resource drop blocks new starts; it never revokes active leases.
            self.pool_size = max(self._slots_used, self._detected_cpus)
            self.thread_pool_size = max(self._threads_used, self._detected_cpus)
            self.memory_pool_mb = max(self._memory_used, self._recommended_memory)
            if old != (self.pool_size, self.thread_pool_size, self.memory_pool_mb):
                self._resize_executor_locked()
                self._condition.notify_all()

    def config(self, *, refresh=False):
        with self._condition:
            self._refresh_resources_locked(force=refresh)
            return {'pool_size': self.pool_size, 'memory_pool_mb': self.memory_pool_mb,
                    'slots_used': self._slots_used, 'memory_used_mb': self._memory_used,
                    'thread_pool_size': self.thread_pool_size, 'thread_used': self._threads_used,
                    'cpu_count': self._detected_cpus, 'data_dir': str(self.data_dir), 'python': self.python,
                    **self._resource_snapshot, 'recommended_memory_pool_mb': self._recommended_memory,
                    'resource_mode': self.resource_mode, 'resource_refresh_seconds': RESOURCE_REFRESH_SECONDS,
                    'resources_checked_at': self._resources_wall_time,
                    'resource_pressure': self.resource_mode == 'auto' and (
                        self._memory_used > self._recommended_memory or self._threads_used > self._detected_cpus),
                    'memory_unreserved_mb': max(0, self.memory_pool_mb - self._memory_used)}

    def configure(self, *, pool_size=None, memory_pool_mb=None, thread_pool_size=None, resource_mode=None):
        with self._condition:
            if self._closed:
                raise RuntimeError('Manager is closed')
            if self._slots_used or any(j.state not in TERMINAL_STATES for j in self._jobs.values()):
                raise RuntimeError('Resource pools can only be changed when all jobs have stopped')
            explicit_limits = any(value is not None for value in (pool_size, memory_pool_mb, thread_pool_size))
            mode = resource_mode if resource_mode is not None else ('manual' if explicit_limits else self.resource_mode)
            if mode not in ('auto', 'manual'):
                raise ValueError('resource_mode must be auto or manual')
            if mode == 'auto' and explicit_limits:
                raise ValueError('Automatic mode detects resource limits; omit fixed pool limits')
            self._refresh_resources_locked(force=True)
            if mode == 'auto':
                payload = {'resource_mode': 'auto'}
            else:
                slots = self.pool_size if pool_size is None else positive_int(pool_size, 'pool_size')
                memory = self.memory_pool_mb if memory_pool_mb is None else positive_int(memory_pool_mb, 'memory_pool_mb')
                threads = self.thread_pool_size if thread_pool_size is None else positive_int(thread_pool_size, 'thread_pool_size')
                if threads > self._detected_cpus:
                    raise ValueError('thread_pool_size exceeds available logical CPUs')
                payload = dict(resource_mode='manual', pool_size=slots, memory_pool_mb=memory, thread_pool_size=threads)
            write_json(self.data_dir / 'config.json', payload)
            self.resource_mode = mode
            if mode == 'manual':
                self.pool_size, self.memory_pool_mb, self.thread_pool_size = slots, memory, threads
                self._resize_executor_locked()
            self._refresh_resources_locked()
            return self.config()

    def submit(self, tgz_file, ref_file, options):
        method = str(options.get('method', 'CCSD(T)')).strip().upper()
        basis = str(options.get('basis', '3zeta')).strip()
        if basis.lower() == 'cbs':
            basis = 'CBS'
        if method not in METHODS or basis not in BASIS:
            raise ValueError('Choose CCSD, CCSD(T), CCSDT, CCSDT(Q) and 3zeta, 4zeta, 5zeta, CBS')
        family = str(options.get('basis_family', 'cc')).strip().lower()
        cbs_pair = str(options.get('cbs_pair', '34')).strip()
        if family not in ('cc', 'aug') or cbs_pair not in ('34', '45'):
            raise ValueError('basis_family must be cc or aug; cbs_pair must be 34 or 45')
        local_method = str(options.get('local_method', 'canonical')).strip().lower()
        if local_method != 'canonical':
            raise ValueError('Only canonical coupled cluster is available; LNO/PNO backends are not installed')
        capability = next(item for item in self.capabilities()['methods'] if item['name'] == method)
        if not capability['available']:
            raise ValueError(capability['reason'])
        slots = positive_int(options.get('pool_size', options.get('thread_pool_size', 1)), 'pool_size')
        threads = positive_int(options.get('task_threads', options.get('threads', 1)), 'task_threads')
        thread_budget = positive_int(options.get('thread_budget', slots * threads), 'thread_budget')
        requested_memory = options.get('memory_pool_mb', options.get('memory_pool_size'))
        if requested_memory is None:
            memory = positive_int(options.get('memory_mb', 4096), 'memory_mb') * slots
        else:
            memory = positive_int(requested_memory, 'memory_pool_mb')
        per_task = positive_int(options.get('memory_mb', memory // slots), 'memory_mb')
        if threads > available_cpus():
            raise ValueError('task_threads exceeds available logical CPUs')
        if slots * threads > thread_budget:
            raise ValueError('pool_size * task_threads exceeds this job thread_budget')
        if per_task * slots > memory:
            raise ValueError('memory_mb * pool_size exceeds this job memory_pool_mb')
        normalized = dict(method=method, basis=basis, pool_size=slots, task_threads=threads,
                          memory_mb=per_task, memory_pool_mb=memory, thread_budget=thread_budget,
                          basis_family=family, cbs_pair=cbs_pair, local_method=local_method)
        archive_name = upload_filename(tgz_file, 'input.tgz')
        ref_name = upload_filename(ref_file, 'input.ref')
        dataset_default = re.sub(r'(?i)\.(tar\.gz|tgz|zip)$', '', archive_name)[:160] or 'dataset'
        dataset = display_name(options.get('dataset'), 'dataset', dataset_default)
        name = display_name(options.get('name'), 'name', dataset)
        with self._condition:
            if self._closed:
                raise RuntimeError('Manager is closed')
            self._refresh_resources_locked(force=True)
            if slots > self.pool_size or memory > self.memory_pool_mb:
                raise ValueError('Job exceeds the configured global pool_size or memory_pool_mb')
            if thread_budget > self.thread_pool_size:
                raise ValueError('Job thread_budget exceeds the configured global thread_pool_size')
            identifier = uuid.uuid4().hex
            directory = self.data_dir / identifier
            directory.mkdir()
            try:
                self._save_upload(tgz_file, directory / 'input.tgz')
                self._save_upload(ref_file, directory / 'input.ref', allow_empty=True)
            except BaseException:
                shutil.rmtree(directory)
                raise
            job = Job(identifier, directory, normalized, name=name, dataset=dataset,
                      archive_filename=archive_name, reference_filename=ref_name)
            try:
                self._persist(job)
                self._jobs[identifier] = job
                job.future = self._executor.submit(self._execute, job)
            except BaseException:
                self._jobs.pop(identifier, None)
                shutil.rmtree(directory)
                raise
            return job

    @staticmethod
    def _save_upload(upload, destination, allow_empty=False):
        if upload is None:
            raise ValueError('Both tgz and ref files are required')
        if hasattr(upload, 'save'):
            upload.save(destination)
        elif isinstance(upload, (bytes, bytearray)):
            destination.write_bytes(upload)
        elif isinstance(upload, (str, os.PathLike)):
            shutil.copyfile(upload, destination)
        elif hasattr(upload, 'read'):
            with destination.open('wb') as target:
                shutil.copyfileobj(upload, target)
        else:
            raise ValueError('Unsupported upload object')
        if not allow_empty and not destination.stat().st_size:
            raise ValueError('Geometry archive cannot be empty')

    def get(self, task_id):
        with self._lock:
            return self._jobs.get(task_id)

    def list_jobs(self, *, limit=50, offset=0, query='', state=None):
        if isinstance(offset, bool) or not re.fullmatch(r'[0-9]+', str(offset)):
            raise ValueError('offset must be a nonnegative integer')
        limit = positive_int(limit, 'limit')
        if limit > 200:
            raise ValueError('limit must not exceed 200')
        if state and state not in ('queued', 'running', 'cancelling', *TERMINAL_STATES):
            raise ValueError('Unknown task state')
        offset, query = int(offset), str(query).casefold()
        with self._lock:
            jobs = [job for job in self._jobs.values()
                    if (not state or job.state == state)
                    and (not query or any(query in str(value).casefold()
                         for value in (job.name, job.dataset, job.task_id, job.options.get('method'))))]
            jobs.sort(key=lambda job: (job.created_at, job.task_id), reverse=True)
            return {'jobs': [job.as_dict() for job in jobs[offset:offset + limit]],
                    'total': len(jobs), 'limit': limit, 'offset': offset}

    def report(self, task_id):
        with self._lock:
            job = self._jobs.get(task_id)
            if job is None:
                return None
            if job.state != 'completed' or job.report is None:
                raise RuntimeError('Task has no complete report')
            result = dict(job.report)
            result.update(task_id=job.task_id, name=job.name, dataset=job.dataset, options=dict(job.options))
            result['reference_rows'] = [
                {'pairs': [{'coefficient': coefficient, 'name': name} for coefficient, name in line.pairs],
                 'value': line.ref_value, 'ratio': line.ratio,
                 'unit': 'kcal/mol' if line.ratio is not None else 'Hartree', 'text': line.original}
                for line in parse_reference(job.output_path.read_text(encoding='utf-8')) if not line.passthrough]
            return result

    def log_tail(self, task_id, *, lines=200):
        lines = positive_int(lines, 'lines')
        if lines > 1000:
            raise ValueError('lines must not exceed 1000')
        with self._lock:
            job = self._jobs.get(task_id)
            if job is None:
                return None
            path, state = job.directory / 'process.log', job.state
        if not path.is_file():
            return {'text': '', 'truncated': False, 'state': state}
        with path.open('rb') as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - 262144)
            handle.seek(start)
            content = handle.read(262144)
        values = content.decode('utf-8', errors='replace').splitlines()
        if start and values:
            values.pop(0)
        return {'text': '\n'.join(values[-lines:]), 'truncated': bool(start or len(values) > lines), 'state': state}

    def cancel(self, task_id):
        with self._condition:
            job = self._jobs.get(task_id)
            if job is None or job.state in TERMINAL_STATES:
                return job
            job.cancel_event.set()
            job.error = 'Cancelled by user'
            if job.started_at is None:
                job.state, job.finished_at = 'cancelled', time.time()
                if job.future:
                    job.future.cancel()
            else:
                job.state = 'cancelling'
            try:
                self._persist(job)
            except OSError as error:
                # Cancellation must still reach the worker when the job disk is full.
                job.error += f'; Cannot persist cancellation: {error}'
            finally:
                self._condition.notify_all()
            return job

    def _execute(self, job):
        slots, memory = job.options['pool_size'], job.options['memory_pool_mb']
        threads = slots * job.options['task_threads']
        process, reserved = None, False
        try:
            with self._condition:
                while True:
                    if job.cancel_event.is_set():
                        return
                    self._refresh_resources_locked()
                    if (self._slots_used + slots <= self.pool_size
                            and self._memory_used + memory <= self.memory_pool_mb
                            and self._threads_used + threads <= self.thread_pool_size):
                        break
                    self._condition.wait(.1)
                self._slots_used += slots
                self._memory_used += memory
                self._threads_used += threads
                reserved = True
                job.state, job.started_at = 'running', time.time()
                self._persist(job)
            request = {'tgz': str(job.directory / 'input.tgz'), 'ref': str(job.directory / 'input.ref'),
                       'output': str(job.output_path),
                       'options': {key: job.options[key] for key in WORKER_OPTIONS if key in job.options}}
            request_path, result_path = job.directory / 'request.json', job.directory / 'report.json'
            write_json(request_path, request)
            with (job.directory / 'process.log').open('w') as log:
                with self._lock:
                    if not job.cancel_event.is_set():
                        process = subprocess.Popen([self.python, '-m', self._worker_module, 'reference', str(request_path), str(result_path)],
                            cwd=job.directory, env=worker_environment(1, job.directory), stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=(os.name == 'posix'))
                        job.process = process
                while process is not None and process.poll() is None:
                    if job.cancel_event.wait(.1):
                        stop_process(process, group=True)
                        break
                    progress_file = job.directory / 'progress.json'
                    if progress_file.exists():
                        with self._lock:
                            job.progress = json.loads(progress_file.read_text())
                if not job.cancel_event.is_set():
                    if process is None or not result_path.exists():
                        raise RuntimeError(f'Worker failed before reporting results (exit {process.returncode if process else None}); see process.log')
                    payload = json.loads(result_path.read_text())
                    if process.returncode or not payload.get('ok'):
                        raise RuntimeError(payload.get('error', 'PySCF worker failed'))
                    report = payload['result']
                    if not report.get('complete') or not job.output_path.is_file() or not job.output_path.stat().st_size:
                        raise RuntimeError('Calculator did not create a complete .ref file')
                    with self._lock:
                        job.report = report
                        job.progress = {'status': 'complete', 'completed': len(report.get('molecularEnergies', []))}
        except Exception as error:
            with self._lock:
                job.error = str(error)
        finally:
            if process is not None:
                try:
                    stop_process(process, group=True)
                except (OSError, subprocess.SubprocessError) as error:
                    job.error = job.error or f'Worker cleanup failed: {error}'
            with self._condition:
                job.process = None
                if job.cancel_event.is_set():
                    job.state = 'cancelled'
                    job.error = 'Cancelled by user'
                elif job.report is not None:
                    job.state = 'completed'
                else:
                    job.state = 'failed'
                if job.state != 'completed':
                    try:
                        job.output_path.unlink(missing_ok=True)
                        for scratch in job.directory.glob('.energyhub-*'):
                            if scratch.is_dir():
                                shutil.rmtree(scratch)
                    except OSError as error:
                        job.error = (job.error or '') + f'; Scratch cleanup failed: {error}'
                job.finished_at = time.time()
                if reserved:
                    self._slots_used -= slots
                    self._memory_used -= memory
                    self._threads_used -= threads
                    self._refresh_resources_locked()
                try:
                    self._persist(job)
                except OSError as error:
                    job.error = (job.error or '') + f'; Cannot persist task metadata: {error}'
                finally:
                    self._condition.notify_all()

    def _persist(self, job):
        write_json(job.directory / 'task.json', job.as_dict())

    def _load_metadata(self, task_id):
        if not re.fullmatch(r'[a-f0-9]{32}', task_id):
            return None
        try:
            data = json.loads((self.data_dir / task_id / 'task.json').read_text())
            options = {key: data[key] for key in ('method', 'basis', 'pool_size', 'task_threads', 'memory_mb', 'memory_pool_mb')}
            for key, default in (('basis_family', 'cc'), ('cbs_pair', '34'), ('local_method', 'canonical'),
                                 ('thread_budget', options['pool_size'] * options['task_threads'])):
                options[key] = data.get(key, default)
            job = Job(task_id, self.data_dir / task_id, options,
                      name=data.get('name', task_id), dataset=data.get('dataset', task_id),
                      archive_filename=data.get('archive_filename', 'input.tgz'),
                      reference_filename=data.get('reference_filename', 'input.ref'))
            for key in ('state', 'created_at', 'started_at', 'finished_at', 'progress', 'error', 'report'):
                setattr(job, key, data.get(key, getattr(job, key)))
            return job
        except (OSError, KeyError, ValueError, TypeError):
            return None

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            for job in self._jobs.values():
                self.cancel(job.task_id)
        self._executor.shutdown(wait=True, cancel_futures=True)
        for executor in self._retired_executors:
            executor.shutdown(wait=True, cancel_futures=True)
        self._directory_lock.close()
