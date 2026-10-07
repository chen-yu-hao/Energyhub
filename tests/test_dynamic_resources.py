"""Live resource changes must affect admission without revoking active work."""
import importlib.util
import json
from unittest import mock

from Energyhub.job_manager import EnergyJobManager
from Energyhub.tests.test_jobs_api import ManagerCase, FAKE_CAPABILITIES


class DynamicResourceTests(ManagerCase):
    def setUp(self):
        super().setUp()
        self.manager.close()
        self.cpus = 4
        self.hardware = {'memory_total_mb':1024, 'memory_capacity_mb':1024, 'memory_available_mb':1024}
        self.memory_patch = mock.patch('Energyhub.job_manager.memory_resources', side_effect=lambda:dict(self.hardware))
        self.cpu_patch = mock.patch('Energyhub.job_manager.available_cpus', side_effect=lambda:self.cpus)
        self.memory_patch.start(); self.cpu_patch.start()
        self.manager = self.create_auto()

    def create_auto(self):
        manager = EnergyJobManager(self.root/'auto-jobs',resource_mode='auto',worker_module='Energyhub.tests.fake_worker')
        manager._capabilities = FAKE_CAPABILITIES
        return manager

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            self.memory_patch.stop(); self.cpu_patch.stop()

    def test_config_resamples_resources_and_updates_auto_limits(self):
        before=self.manager.config()
        self.assertEqual(before['resource_mode'],'auto')
        self.assertEqual((before['thread_pool_size'],before['memory_pool_mb']),(4,768))
        self.hardware['memory_available_mb']=256;self.cpus=2
        after=self.manager.config(refresh=True)
        self.assertEqual((after['cpu_count'],after['thread_pool_size'],after['pool_size']),(2,2,2))
        self.assertEqual(after['memory_pool_mb'],204)
        self.assertEqual(after['memory_available_mb'],256)
        self.assertGreaterEqual(after['resources_checked_at'],before['resources_checked_at'])

    def test_auto_cache_expires_without_explicit_refresh(self):
        self.hardware['memory_available_mb']=512
        self.assertEqual(self.manager.config()['memory_pool_mb'],768)
        self.manager._resources_checked_at=0
        self.assertEqual(self.manager.config()['memory_pool_mb'],256)

    def test_memory_pressure_preserves_running_lease_and_queued_work_recovers(self):
        self.hardware['memory_available_mb']=512
        self.manager.config(refresh=True)
        gate=self.root/'release'
        first=self.submit({'gate':str(gate)},memory_mb=192)
        self.started(first)
        pid=first.process.pid
        second=self.submit(memory_mb=128)
        self.wait(lambda:second.state=='queued')
        self.hardware['memory_available_mb']=128
        config=self.manager.config(refresh=True)
        self.assertTrue(config['resource_pressure'])
        self.assertEqual(config['memory_pool_mb'],192)
        self.assertEqual(first.options['memory_mb'],192)
        self.assertEqual(first.process.pid,pid)
        self.assertEqual(first.state,'running')
        self.assertFalse((second.directory/'worker.pid').exists())
        # The scheduler must notice recovery even with no browser/config polling.
        self.hardware['memory_available_mb']=1024
        self.wait(lambda:(second.directory/'worker.pid').exists(),timeout=6)
        self.assertEqual(self.terminal(second).state,'completed')
        self.assertEqual(first.state,'running')
        gate.touch()
        self.assertEqual(self.terminal(first).state,'completed')

    def test_cpu_resize_migrates_pending_work_and_never_overbooks_after_shrink(self):
        self.cpus=2;self.manager.config(refresh=True)
        gate=self.root/'cpu-release'
        jobs=[self.submit({'gate':str(gate)}) for _ in range(4)]
        self.started(jobs[0]);self.started(jobs[1])
        original=[jobs[0].process.pid,jobs[1].process.pid]
        self.assertFalse((jobs[2].directory/'worker.pid').exists())
        self.cpus=4;self.manager.config(refresh=True)
        self.started(jobs[2]);self.started(jobs[3])
        self.assertEqual([jobs[0].process.pid,jobs[1].process.pid],original)
        self.cpus=1
        config=self.manager.config(refresh=True)
        self.assertTrue(config['resource_pressure'])
        self.assertEqual(config['thread_used'],4)
        self.assertEqual(config['thread_pool_size'],4)
        fifth=self.submit()
        self.assertEqual(fifth.state,'queued')
        self.assertFalse((fifth.directory/'worker.pid').exists())
        gate.touch()
        for job in jobs+[fifth]:
            self.assertEqual(self.terminal(job).state,'completed')
        self.assertEqual(self.manager.config()['thread_pool_size'],1)

    def test_manual_limits_stay_fixed_but_telemetry_remains_live(self):
        self.manager.configure(resource_mode='manual',memory_pool_mb=384,pool_size=2,thread_pool_size=2)
        self.hardware['memory_available_mb']=128
        result=self.manager.config(refresh=True)
        self.assertEqual(result['resource_mode'],'manual')
        self.assertEqual(result['memory_pool_mb'],384)
        self.assertEqual(result['memory_available_mb'],128)

    def test_auto_persistence_does_not_save_machine_specific_numbers(self):
        self.manager.configure(memory_pool_mb=64,pool_size=1,thread_pool_size=1)
        self.manager.configure(resource_mode='auto')
        saved=json.loads((self.manager.data_dir/'config.json').read_text())
        self.assertEqual(saved,{'resource_mode':'auto'})
        self.manager.close()
        self.cpus=8;self.hardware['memory_available_mb']=512
        self.manager=EnergyJobManager(self.root/'auto-jobs',worker_module='Energyhub.tests.fake_worker')
        self.assertEqual(self.manager.config()['resource_mode'],'auto')
        self.assertEqual((self.manager.pool_size,self.manager.memory_pool_mb),(8,256))

    def test_mode_validation_does_not_silently_ignore_fixed_limits(self):
        for options in [{'resource_mode':'unknown'},{'resource_mode':'auto','memory_pool_mb':128}]:
            with self.subTest(options=options),self.assertRaises(ValueError):
                self.manager.configure(**options)

    def test_http_refresh_and_mode_switch(self):
        if not importlib.util.find_spec('flask'):
            self.skipTest('Flask is optional')
        from Energyhub.api import create_app
        client=create_app(self.manager).test_client()
        self.hardware['memory_available_mb']=256
        info=client.get('/api/energyhub/config?refresh=1').get_json()
        self.assertEqual(info['memory_pool_mb'],204)
        response=client.put('/api/energyhub/config',json={'resource_mode':'manual','memory_pool_mb':128})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['resource_mode'],'manual')
        response=client.put('/api/energyhub/config',json={'resource_mode':'auto'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['resource_mode'],'auto')
