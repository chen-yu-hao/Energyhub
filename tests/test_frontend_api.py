"""Frontend contracts, saved metadata, and scheduler resource regressions."""
from __future__ import annotations

import io
import importlib.util
import json
import subprocess
import unittest
from unittest import mock

from Energyhub.job_manager import EnergyJobManager
from Energyhub.tests.test_jobs_api import FAKE_CAPABILITIES, ManagerCase


@unittest.skipUnless(importlib.util.find_spec('flask'), 'optional Flask extra is unavailable')
class FrontendApiTests(ManagerCase):
    def setUp(self):
        super().setUp()
        from Energyhub.api import create_app
        self.app = create_app(self.manager)
        self.app.testing = True
        self.client = self.app.test_client()

    def post(self, **options):
        data = {'tgz': (io.BytesIO(b'{}'), 'water cluster.tar.gz'),
                'ref': (io.BytesIO(b''), 'template.ref'), 'memory_mb': '64'}
        data.update(options)
        response = self.client.post('/api/energyhub/jobs', data=data)
        return response

    def test_example_archive_is_not_http_transport_encoded(self):
        response=self.client.get('/static/examples/H2.tgz')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('Content-Encoding',response.headers)
        self.assertEqual(response.mimetype,'application/gzip')
        self.assertEqual(response.data[:2],b'\x1f\x8b')
        response.close()

    def test_task_names_history_search_and_download_survive_restart(self):
        response = self.post(name='水簇的参考能', dataset='WATER27')
        self.assertEqual(response.status_code, 202, response.get_json())
        job = self.terminal(self.manager.get(response.get_json()['task_id']))
        self.assertEqual(job.name, '水簇的参考能')
        self.assertEqual(job.archive_filename, 'water cluster.tar.gz')
        self.assertEqual(job.reference_filename, 'template.ref')
        second = self.terminal(self.submit(name='second', dataset='Other'))
        listing = self.client.get('/api/energyhub/jobs?limit=1').get_json()
        self.assertEqual(listing['total'], 2)
        self.assertEqual(listing['jobs'][0]['task_id'], second.task_id)
        self.assertEqual(self.client.get('/api/energyhub/jobs?limit=1&offset=1').get_json()['jobs'][0]['task_id'], job.task_id)
        filtered = self.client.get('/api/energyhub/jobs?q=water&state=completed').get_json()
        self.assertEqual([item['task_id'] for item in filtered['jobs']], [job.task_id])
        self.make_manager()
        self.assertEqual(self.manager.get(job.task_id).name, '水簇的参考能')
        from Energyhub.api import create_app
        client = create_app(self.manager).test_client()
        result = client.get(f'/api/energyhub/jobs/{job.task_id}/result')
        self.assertIn('WATER27.ref', result.headers['Content-Disposition'])
        result.close()

    def test_report_provides_reference_rows_and_json_download(self):
        job = self.terminal(self.manager.get(self.post(dataset='example').get_json()['task_id']))
        url = f'/api/energyhub/jobs/{job.task_id}/report'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['reference_rows'][0]['pairs'], [{'coefficient': 1.0, 'name': 'H2'}])
        self.assertEqual(payload['reference_rows'][0]['value'], -1.1)
        self.assertEqual(payload['reference_rows'][0]['unit'], 'Hartree')
        self.assertEqual(payload['dataset'], 'example')
        downloaded = self.client.get(url + '?download=1')
        self.assertIn('example.json', downloaded.headers['Content-Disposition'])
        self.assertEqual(json.loads(downloaded.data)['task_id'], job.task_id)
        downloaded.close()

    def test_log_tail_is_bounded_and_missing_resources_are_explicit(self):
        job = self.terminal(self.submit())
        (job.directory / 'process.log').write_text('first\nsecond\nlast\n', encoding='utf-8')
        response = self.client.get(f'/api/energyhub/jobs/{job.task_id}/log?lines=2')
        self.assertEqual(response.get_json()['text'], 'second\nlast')
        self.assertTrue(response.get_json()['truncated'])
        self.assertEqual(self.client.get(f'/api/energyhub/jobs/{job.task_id}/log?lines=1001').status_code, 400)
        for suffix in ('report', 'log'):
            self.assertEqual(self.client.get('/api/energyhub/jobs/' + '0' * 32 + '/' + suffix).status_code, 404)

    def test_rejects_invalid_names_queries_and_local_methods(self):
        for options in ({'name': 'bad\nname'}, {'name': 'x' * 161}, {'dataset': ''},
                        {'basis_family': 'def2'}, {'cbs_pair': '56'}, {'local_method': 'lno'}):
            with self.subTest(options=options):
                self.assertEqual(self.post(**options).status_code, 400)
        for query in ('limit=201', 'limit=0', 'offset=-1', 'state=unknown'):
            self.assertEqual(self.client.get('/api/energyhub/jobs?' + query).status_code, 400)
        self.assertEqual(self.manager._jobs, {})

    def test_ui_metadata_is_not_forwarded_to_compute_reference(self):
        response = self.post(name='title', dataset='set', method='CCSD', basis='5zeta',
                             basis_family='aug', cbs_pair='45', thread_budget='1')
        self.assertEqual(response.status_code, 202)
        job = self.terminal(self.manager.get(response.get_json()['task_id']))
        options = json.loads((job.directory / 'received-options.json').read_text())
        self.assertEqual(options['basis'], '5zeta')
        self.assertEqual(options['basis_family'], 'aug')
        self.assertEqual(options['cbs_pair'], '45')
        for key in ('name', 'dataset', 'thread_budget', 'local_method'):
            self.assertNotIn(key, options)

    def test_capability_timeout_is_json_and_explicit_refresh_reloads(self):
        with mock.patch('Energyhub.job_manager.subprocess.run', side_effect=subprocess.TimeoutExpired('fixture', 30)):
            response = self.client.get('/api/energyhub/methods?refresh=1')
            self.assertEqual(response.status_code, 503)
            self.assertIn('timed out', response.get_json()['error'])
        capabilities = {**FAKE_CAPABILITIES, 'pyscf_version': 'upgraded'}
        with mock.patch('Energyhub.job_manager.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(capabilities), '')) as run:
            response = self.client.get('/api/energyhub/methods?refresh=1')
            self.assertEqual(response.get_json()['pyscf_version'], 'upgraded')
            self.client.get('/api/energyhub/methods')
            run.assert_called_once()

    def test_config_reports_cpu_limits_and_persists_across_restart(self):
        response = self.client.get('/api/energyhub/config')
        self.assertGreaterEqual(response.get_json()['cpu_count'], 1)
        self.assertGreater(response.get_json()['max_upload_bytes'], 0)
        changed = self.client.put('/api/energyhub/config', json={'pool_size': 3, 'thread_pool_size': 1, 'memory_pool_mb': 192})
        self.assertEqual(changed.status_code, 200)
        self.manager.close()
        self.manager = EnergyJobManager(self.root / 'jobs', worker_module='Energyhub.tests.fake_worker')
        self.assertEqual(self.manager.config()['pool_size'], 3)
        self.assertEqual(self.manager.config()['thread_pool_size'], 1)
        self.assertEqual(self.manager.config()['memory_pool_mb'], 192)

    def test_frontend_and_static_files_are_served_from_the_package(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<html', response.data.lower())
        response.close()
        self.assertEqual(self.client.get('/static/../job_manager.py').status_code, 404)


class SchedulerResourceTests(ManagerCase):
    def test_global_thread_budget_blocks_when_memory_and_slots_are_available(self):
        self.manager.configure(thread_pool_size=1)
        gate = self.root / 'release'
        first = self.submit({'gate': str(gate)})
        self.started(first)
        second = self.submit()
        self.wait(lambda: second.state == 'queued')
        self.assertEqual(self.manager.config()['thread_used'], 1)
        self.assertFalse((second.directory / 'worker.pid').exists())
        gate.touch()
        self.assertEqual(self.terminal(first).state, 'completed')
        self.assertEqual(self.terminal(second).state, 'completed')
        self.assertEqual(self.manager.config()['thread_used'], 0)

    def test_resource_lease_released_if_running_metadata_cannot_be_saved(self):
        original = self.manager._persist
        def fail_running(job):
            if job.state == 'running':
                raise OSError('fixture disk error')
            original(job)
        with mock.patch.object(self.manager, '_persist', side_effect=fail_running):
            job = self.terminal(self.submit())
        self.assertEqual(job.state, 'failed')
        self.assertIn('fixture disk error', job.error)
        config = self.manager.config()
        self.assertEqual((config['slots_used'], config['memory_used_mb'], config['thread_used']), (0, 0, 0))
        self.assertEqual(self.terminal(self.submit()).state, 'completed')
