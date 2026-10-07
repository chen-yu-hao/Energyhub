"""Real tgz/ref workflows using the existing PySCF subprocess environment."""
import io
import json
import math
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
from Energyhub.core import CalculationCancelled, CalculationError, compute_reference, _safe_extract, _read_xyz
from Energyhub.worker import extrapolate

ROOT = Path(__file__).parent


def archive_bytes():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, bond in [('H2', .74), ('H2_stretched', 1.4)]:
            data = f'2\n0 1\nH 0 0 0\nH 0 0 {bond}\n'.encode()
            member = tarfile.TarInfo('nested/' + name + '.xyz')
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return stream.getvalue()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'demo.tgz'
        self.archive.write_bytes(archive_bytes())
        self.reference = self.root / 'demo.ref'
        self.reference.write_text('1 H2_stretched -1 H2 ? 2\n')
        self.output = self.root / 'complete.ref'

    def test_real_ccsdt_parallel_ref_and_molecular_provenance(self):
        progress = []
        report = compute_reference(self.archive, self.reference, self.output,
            method='CCSDT', basis='3zeta', pool_size=2, task_threads=1,
            memory_pool_mb=1024, progress=progress.append)
        self.assertTrue(report.complete)
        values = {row.name: row.energy_hartree for row in report.results}
        fields = self.output.read_text().split()
        self.assertEqual(fields[-1], '2')
        self.assertAlmostEqual(float(fields[-2]), (values['H2_stretched'] - values['H2']) * 627.509, places=8)
        self.assertTrue(all(row.details['converged'] for row in report.results))
        self.assertTrue(any(row.get('completed') == 2 for row in progress))
        self.assertIn('?', self.reference.read_text())

    def test_cbs_extrapolates_components_independently(self):
        exact_hf, exact_corr, alpha = -1.0, -.3, 1.63
        def point(n):
            return {'hf_hartree': exact_hf + .5 * math.exp(-alpha*n),
                    'correlation_hartree': exact_corr + .7/n**3,
                    'pyscf_version': 'fixture', 'reference_type': 'RHF'}
        result = extrapolate(point(3), point(4))
        self.assertAlmostEqual(result['hf_hartree'], exact_hf, places=13)
        self.assertAlmostEqual(result['correlation_hartree'], exact_corr, places=13)
        self.assertAlmostEqual(result['energy_hartree'], -1.3, places=13)

    def test_missing_molecule_and_malformed_input_do_not_publish(self):
        self.reference.write_text('1 missing ? 1\n')
        with self.assertRaises(CalculationError):
            compute_reference(self.archive, self.reference, self.output)
        self.assertFalse(self.output.exists())
        self.reference.write_text('this is not a reference\n')
        with self.assertRaises(CalculationError):
            compute_reference(self.archive, self.reference, self.output)
        self.assertFalse(self.output.exists())

    def test_pre_cancelled_request_does_not_publish(self):
        event = threading.Event(); event.set()
        with self.assertRaises(CalculationCancelled):
            compute_reference(self.archive, self.reference, self.output, cancel_event=event)
        self.assertFalse(self.output.exists())

    def test_archive_escape_and_link_rejected(self):
        for name, link in [('../escaped.xyz', False), ('alias.xyz', True)]:
            with tarfile.open(self.archive, 'w:gz') as archive:
                member = tarfile.TarInfo(name)
                if link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = 'target.xyz'
                archive.addfile(member)
            with self.assertRaises(CalculationError):
                _safe_extract(self.archive, self.root / 'extracted')

    def test_nonfinite_coordinates_and_invalid_spin_header_rejected(self):
        xyz = self.root / 'bad.xyz'
        for text in ['1\n0 2\nH NaN 0 0\n', '1\ncomment\nH 0 0 0\n']:
            xyz.write_text(text)
            with self.assertRaises(CalculationError):
                _read_xyz(xyz)

    def test_real_http_closed_shell_quadruples(self):
        try:
            from Energyhub.api import create_app
        except ImportError:
            self.skipTest('Run HTTP workflow with the existing hxweb interpreter')
        app = create_app(data_dir=self.root / 'q-jobs', memory_pool_mb=512)
        manager = app.extensions['energyhub_manager']
        self.addCleanup(manager.close)
        client = app.test_client()
        capabilities = client.get('/api/energyhub/methods').get_json()
        q = next(row for row in capabilities['methods'] if row['name'] == 'CCSDT(Q)')
        if not q['closed_shell']:
            self.skipTest('Installed PySCF has no RHF-CCSDT(Q) implementation')
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            geometry = b'1\n0 1\nBe 0 0 0\n'
            member = tarfile.TarInfo('Be.xyz')
            member.size = len(geometry)
            archive.addfile(member, io.BytesIO(geometry))
        response = client.post('/api/energyhub/jobs', data={
            'tgz': (io.BytesIO(stream.getvalue()), 'be.tgz'),
            'ref': (io.BytesIO(b'1 Be ? 1\n'), 'be.ref'),
            'method': 'CCSDT(Q)', 'basis': '3zeta',
            'memory_pool_mb': '512', 'task_threads': '1'}, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 202, response.get_json())
        url = '/api/energyhub/jobs/' + response.get_json()['task_id']
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = client.get(url).get_json()
            if status['state'] in {'completed', 'failed', 'cancelled'}:
                break
            time.sleep(.1)
        self.assertEqual(status['state'], 'completed', status)
        row = status['report']['molecularEnergies'][0]
        self.assertEqual(row['method'], 'CCSDT(Q)')
        self.assertLess(row['details']['perturbative_correction_hartree'], -1e-9)
        self.assertEqual(row['details']['pyscf_version'], capabilities['pyscf_version'])
        download = client.get(url + '/result')
        self.addCleanup(download.close)
        self.assertEqual(download.status_code, 200)
        self.assertAlmostEqual(float(download.data.split()[-2]), row['energy_hartree'] * 627.509, places=8)

    def test_real_http_cbs_uses_pyscf_environment_and_downloads_complete_ref(self):
        try:
            from Energyhub.api import create_app
        except ImportError:
            self.skipTest('Run HTTP workflow with the existing hxweb interpreter')
        app = create_app(data_dir=self.root / 'jobs', pool_size=2, memory_pool_mb=1024)
        manager = app.extensions['energyhub_manager']
        self.addCleanup(manager.close)
        client = app.test_client()
        capabilities = client.get('/api/energyhub/methods').get_json()
        self.assertTrue(next(row for row in capabilities['methods'] if row['name'] == 'CCSDT')['available'])
        response = client.post('/api/energyhub/jobs', data={
            'tgz': (io.BytesIO(archive_bytes()), 'demo.tgz'),
            'ref': (io.BytesIO(b'1 H2_stretched -1 H2 ? 2\n'), 'demo.ref'),
            'method': 'CCSD(T)', 'basis': 'CBS', 'pool_size': '2',
            'memory_pool_mb': '1024', 'task_threads': '1'}, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 202, response.get_json())
        url = '/api/energyhub/jobs/' + response.get_json()['task_id']
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = client.get(url).get_json()
            if status['state'] in {'completed', 'failed', 'cancelled'}:
                break
            time.sleep(.1)
        self.assertEqual(status['state'], 'completed', status)
        report = status['report']
        values = {row['name']: row['energy_hartree'] for row in report['molecularEnergies']}
        download = client.get(url + '/result')
        self.addCleanup(download.close)
        self.assertEqual(download.status_code, 200)
        self.assertAlmostEqual(float(download.data.split()[-2]), (values['H2_stretched'] - values['H2']) * 627.509, places=8)
        for row in report['molecularEnergies']:
            self.assertEqual(set(row['details']['components']), {'cc-pvtz', 'cc-pvqz'})
            self.assertEqual(row['details']['pyscf_version'], capabilities['pyscf_version'])


if __name__ == '__main__':
    unittest.main()
