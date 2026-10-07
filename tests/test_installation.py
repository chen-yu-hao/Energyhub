"""Portable runtime selection, CLI contracts and relocated worker imports."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from Energyhub import __version__
from Energyhub import cli, runtime

PACKAGE = Path(__file__).resolve().parents[1]


class PortableRuntimeTests(unittest.TestCase):
    def test_explicit_interpreter_overrides_discovery(self):
        with mock.patch.dict(os.environ, {'ENERGYHUB_PYTHON': sys.executable}), mock.patch.object(runtime, '_python_candidates') as discover:
            self.assertEqual(runtime.python_executable(), str(Path(sys.executable).absolute()))
            discover.assert_not_called()

    def test_invalid_explicit_interpreter_has_actionable_error(self):
        with mock.patch.dict(os.environ, {'ENERGYHUB_PYTHON': '/missing/energyhub/python'}):
            with self.assertRaisesRegex(RuntimeError, 'ENERGYHUB_PYTHON'):
                runtime.python_executable()

    def test_prefer_capable_environment_over_older_web_python(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(runtime, '_python_candidates', return_value=['web', 'compute']), mock.patch.object(runtime, '_pyscf_version', side_effect=[(2, 9, 0), (2, 14, 0)]):
            self.assertEqual(runtime.python_executable(), 'compute')

    def test_current_capable_python_does_not_probe_other_environments(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(runtime, '_python_candidates', return_value=['current', 'other']), mock.patch.object(runtime, '_pyscf_version', return_value=(2, 14, 0)) as probe:
            self.assertEqual(runtime.python_executable(), 'current')
            probe.assert_called_once_with('current')

    def test_data_directory_follows_user_configuration_without_creating_it(self):
        with tempfile.TemporaryDirectory(dir=PACKAGE / 'tests', prefix='.install-test-') as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(root / 'xdg')}, clear=True):
                self.assertEqual(runtime.default_data_dir(), root / 'xdg' / 'energyhub')
                self.assertFalse(runtime.default_data_dir().exists())
            with mock.patch.dict(os.environ, {'ENERGYHUB_DATA_DIR': str(root / 'chosen')}, clear=True):
                self.assertEqual(runtime.default_data_dir(), root / 'chosen')

    def test_relocated_package_does_not_expose_host_site_packages_to_worker(self):
        with tempfile.TemporaryDirectory(dir=PACKAGE / 'tests', prefix='.install-test-') as directory:
            root = Path(directory)
            host = root / 'web-site-packages'
            moved = host / 'Energyhub'
            moved.mkdir(parents=True)
            for source in PACKAGE.glob('*.py'):
                shutil.copy2(source, moved / source.name)
            shutil.copytree(PACKAGE / '_bootstrap', moved / '_bootstrap', ignore=shutil.ignore_patterns('__pycache__'))
            (host / 'host_only_energyhub_fixture.py').write_text('HOST_ABI = True\n')
            spec = importlib.util.spec_from_file_location('relocated_runtime', moved / 'runtime.py')
            relocated = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(relocated)
            with mock.patch.dict(os.environ, {'PYTHONPATH': ''}):
                env = relocated.worker_environment()
            script = "import json, importlib.util, Energyhub; print(json.dumps({'file': Energyhub.__file__, 'version': Energyhub.__version__, 'host_dependency': bool(importlib.util.find_spec('host_only_energyhub_fixture'))}))"
            result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            loaded = json.loads(result.stdout)
            self.assertEqual(Path(loaded['file']), moved / '__init__.py')
            self.assertEqual(loaded['version'], __version__)
            self.assertFalse(loaded['host_dependency'])
            self.assertNotIn(str(host), env['PYTHONPATH'].split(os.pathsep))


class PortableCommandTests(unittest.TestCase):
    def test_serve_defaults_preserve_saved_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            parsed = cli._parser().parse_args(['serve'])
        self.assertIsNone(parsed.pool_size)
        self.assertIsNone(parsed.memory_pool_mb)
        self.assertIsNone(parsed.thread_pool_size)
        self.assertIsNone(parsed.data_dir)

    def test_run_cli_passes_scientific_and_resource_options(self):
        report = mock.Mock()
        report.to_dict.return_value = {'complete': True}
        with mock.patch('Energyhub.core.compute_reference', return_value=report) as calculate, contextlib.redirect_stdout(io.StringIO()):
            status = cli.main(['run', 'dataset.tgz', '--ref', 'template.ref', '--output', 'computed.ref', '--method', 'CCSDT(Q)', '--basis', 'CBS', '--basis-family', 'aug', '--cbs-pair', '45', '--pool-size', '2', '--task-threads', '3', '--memory-mb', '2048', '--memory-pool-mb', '4096'])
        self.assertEqual(status, 0)
        positional, options = calculate.call_args
        self.assertEqual(positional, (Path('dataset.tgz'), Path('template.ref'), Path('computed.ref')))
        self.assertEqual(options['basis_family'], 'aug')
        self.assertEqual(options['cbs_pair'], '45')
        self.assertEqual(options['task_threads'], 3)
        self.assertEqual(options['memory_pool_mb'], 4096)

    def test_doctor_json_works_from_external_working_directory(self):
        with tempfile.TemporaryDirectory(dir=PACKAGE / 'tests', prefix='.install-test-') as directory:
            env = runtime.worker_environment()
            env['ENERGYHUB_PYTHON'] = sys.executable
            env['ENERGYHUB_DATA_DIR'] = str(Path(directory) / 'data')
            result = subprocess.run([sys.executable, '-m', 'Energyhub', 'doctor', '--json'], cwd=directory, env=env, capture_output=True, text=True, timeout=30)
            self.assertIn(result.returncode, (0, 1), result.stderr)
            details = json.loads(result.stdout)
            self.assertEqual(details['energyhub_version'], __version__)
            self.assertEqual(details['python'], str(Path(sys.executable).absolute()))
            self.assertEqual(details['data_dir'], env['ENERGYHUB_DATA_DIR'])
            self.assertFalse(Path(details['data_dir']).exists())


if __name__ == '__main__':
    unittest.main()
