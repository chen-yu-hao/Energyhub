"""Machine-aware defaults must replace arbitrary eight-GB/one-slot defaults."""
import tempfile
from pathlib import Path
import unittest
from unittest import mock
from Energyhub.resource_limits import default_memory_pool_mb, memory_resources
from Energyhub.job_manager import EnergyJobManager


class ResourceDefaultTests(unittest.TestCase):
    def test_budget_uses_available_memory_and_reserves_headroom(self):
        self.assertEqual(default_memory_pool_mb({'memory_capacity_mb': 65536, 'memory_available_mb': 53248}), 40960)
        self.assertEqual(default_memory_pool_mb({'memory_capacity_mb': 8192, 'memory_available_mb': 3072}), 2304)
        self.assertEqual(default_memory_pool_mb({'memory_capacity_mb': 512, 'memory_available_mb': 100}), 80)
        self.assertEqual(default_memory_pool_mb({'memory_capacity_mb': None, 'memory_available_mb': None}), 8192)

    def test_container_limit_reduces_host_capacity_and_available_memory(self):
        with mock.patch('Energyhub.resource_limits._linux_memory', return_value=(64*1024**3, 50*1024**3)), mock.patch('Energyhub.resource_limits._cgroup_memory',return_value=(4*1024**3, 2*1024**3)):
            result=memory_resources()
        self.assertEqual(result, {'memory_total_mb':65536,'memory_capacity_mb':4096,'memory_available_mb':2048})

    def test_unknown_available_memory_is_not_reported_as_total_memory(self):
        with mock.patch('Energyhub.resource_limits._linux_memory',return_value=(64*1024**3,None)), mock.patch('Energyhub.resource_limits._cgroup_memory',return_value=(None,None)):
            result=memory_resources()
        self.assertIsNone(result['memory_available_mb'])
        self.assertEqual(result['memory_capacity_mb'],65536)
        self.assertEqual(default_memory_pool_mb(result),49152)

    def test_defaults_and_explicit_saved_resource_limits(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            with mock.patch('Energyhub.job_manager.available_cpus',return_value=16), mock.patch('Energyhub.job_manager.default_memory_pool_mb',return_value=32768):
                manager=EnergyJobManager(directory)
                self.assertEqual(manager.pool_size,16)
                self.assertEqual(manager.memory_pool_mb,32768)
                manager.configure(pool_size=4,memory_pool_mb=12288,thread_pool_size=8)
                manager.close()
                manager=EnergyJobManager(directory)
                self.assertEqual((manager.pool_size,manager.memory_pool_mb,manager.thread_pool_size),(4,12288,8))
                manager.close()

if __name__=='__main__':
    unittest.main()
