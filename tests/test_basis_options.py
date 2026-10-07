"""Basis-family/cardinality contracts and CBS extrapolation regressions."""
import io
import json
import math
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
from Energyhub.core import CalculationError, CalculationSettings, compute_reference
from Energyhub.worker import basis_pair, extrapolate


class BasisOptionTests(unittest.TestCase):
    def test_every_finite_basis_preset_maps_to_exact_family(self):
        for basis, label in [('3zeta', 't'), ('4zeta', 'q'), ('5zeta', '5')]:
            for family, prefix in [('cc', ''), ('aug', 'aug-')]:
                with self.subTest(basis=basis, family=family):
                    settings=CalculationSettings(basis=basis, basis_family=family)
                    self.assertEqual(settings.basis, prefix+'cc-pv'+label+'z')
                    self.assertEqual(settings.basis_family, family)
        self.assertEqual(CalculationSettings(basis='aug-cc-pv5z').basis_family, 'aug')

    def test_invalid_family_and_cbs_pair_are_not_silently_replaced(self):
        for options in [{'basis_family':'other'}, {'cbs_pair':'35'}, {'basis':'2zeta'}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                CalculationSettings(**options)

    def test_cbs_45_and_augmented_provenance(self):
        def point(cardinal):
            return dict(hf_hartree=-1+2*math.exp(-1.63*cardinal),
                        correlation_hartree=-.2+3/cardinal**3,
                        pyscf_version='fixture', reference_type='RHF')
        for pair in ['34', '45']:
            for family in ['cc', 'aug']:
                lo,hi=map(int,pair)
                result=extrapolate(point(lo),point(hi),pair=pair,family=family)
                self.assertAlmostEqual(result['energy_hartree'],-1.2,places=12)
                self.assertEqual(set(result['components']),set(basis_pair(pair,family)))
                self.assertEqual(result['cbs']['pair'],pair)
                self.assertEqual(result['cbs']['basis_family'],family)

    def test_live_augmented_basis_and_cbs45_include_real_five_zeta(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root=Path(temporary)
            archive=root/'hydrogen.tgz'
            with tarfile.open(archive,'w:gz') as handle:
                geometry=b'1\n0 2\nH 0 0 0\n'
                member=tarfile.TarInfo('H.xyz');member.size=len(geometry)
                handle.addfile(member,io.BytesIO(geometry))
            reference=root/'H.ref';reference.write_text('1 H ? 1\n')
            augmented=compute_reference(archive,reference,root/'aug.ref',method='CCSD',
                                        basis='3zeta',basis_family='aug',memory_mb=512)
            self.assertEqual(augmented.results[0].basis,'aug-cc-pvtz')
            self.assertLess(abs(augmented.results[0].energy_hartree+.5),.001)
            cbs=compute_reference(archive,reference,root/'cbs.ref',method='CCSD',
                                  basis='CBS',cbs_pair='45',memory_mb=512)
            self.assertEqual(set(cbs.results[0].details['components']),{'cc-pvqz','cc-pv5z'})
            self.assertLess(abs(cbs.results[0].energy_hartree+.5),.001)
            self.assertEqual(cbs.settings['cbs_pair'],'45')

    def test_open_shell_q_fails_before_any_molecule_worker(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root=Path(temporary)
            archive=root/'atoms.tgz'
            with tarfile.open(archive,'w:gz') as handle:
                geometry=b'1\n0 2\nH 0 0 0\n'
                member=tarfile.TarInfo('H.xyz');member.size=len(geometry)
                handle.addfile(member,io.BytesIO(geometry))
            reference=root/'atoms.ref';reference.write_text('1 H ? 1\n')
            probe=mock.Mock(returncode=0,stdout=json.dumps({'methods':[{'name':'CCSDT(Q)','closed_shell':True,'open_shell':False,'reason':None}]}))
            with mock.patch('Energyhub.core.subprocess.run',return_value=probe), mock.patch('Energyhub.core.subprocess.Popen') as worker:
                with self.assertRaisesRegex(CalculationError,'H: CCSDT\\(Q\\) is unavailable for open shell'):
                    compute_reference(archive,reference,root/'result.ref',method='CCSDT(Q)')
                worker.assert_not_called()
                self.assertFalse((root/'result.ref').exists())

if __name__=='__main__':
    unittest.main()
