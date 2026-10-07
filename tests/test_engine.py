"""Method identity and convergence regressions for reference energies."""

from __future__ import annotations

import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from Energyhub import core
from Energyhub.capabilities import _implemented_method, get_capabilities


def molecule(name="H2", multiplicity=1):
    return core.Molecule(name, Path(name + ".xyz"), 0, multiplicity, "H 0 0 0\nH 0 0 1.4", 2)


class EngineDispatchTests(unittest.TestCase):
    def setUp(self):
        self.mf = mock.Mock(converged=True, e_tot=-10.0)
        self.runner = mock.Mock(converged=True, e_corr=-0.2, tamps=("t1", "t2", "t3"))
        self.runner.ccsd_t.return_value = -0.03
        self.runner.ccsdt_q.return_value = (-0.003, -0.004)
        self.cc = types.SimpleNamespace(CCSD=mock.Mock(return_value=self.runner), CCSDT=mock.Mock(return_value=self.runner), CCSDTQ=mock.Mock(side_effect=AssertionError("full CCSDTQ must never be substituted")))
        self.scf = types.SimpleNamespace(RHF=mock.Mock(return_value=self.mf), UHF=mock.Mock(return_value=self.mf))
        self.fake = types.ModuleType("pyscf")
        self.fake.__version__ = "test"
        self.fake.cc, self.fake.scf = self.cc, self.scf
        self.fake.gto = types.SimpleNamespace(M=mock.Mock())
        self.fake.lib = types.SimpleNamespace(num_threads=mock.Mock())
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict("sys.modules", {"pyscf": self.fake}).start()
        mock.patch.object(core, "_basis_for", return_value=({"H": "fixture"}, None)).start()

    def energy(self, method, spin=1):
        return core._cc_energy(molecule(multiplicity=spin), method, "cc-pvtz", 512, 2, None)

    def test_ccsdt_dispatch_and_convergence_tolerances(self):
        result = self.energy("CCSDT")
        self.cc.CCSDT.assert_called_once_with(self.mf)
        self.cc.CCSD.assert_not_called()
        self.cc.CCSDTQ.assert_not_called()
        self.runner.ccsd_t.assert_not_called()
        self.assertAlmostEqual(result["energy_hartree"], -10.2)
        self.assertEqual(result["hf_hartree"], -10.0)
        self.assertEqual(result["correlation_hartree"], -0.2)
        self.assertEqual(self.mf.conv_tol, 1e-10)
        self.assertEqual(self.runner.conv_tol, 1e-9)
        self.assertEqual(self.runner.conv_tol_normt, 1e-7)
        self.assertEqual(self.runner.max_memory, 512)
        self.fake.lib.num_threads.assert_called_once_with(2)

    def test_nonzero_triples_added_once(self):
        result = self.energy("CCSD(T)")
        self.assertAlmostEqual(result["energy_hartree"], -10.23)
        self.assertAlmostEqual(result["correlation_hartree"], -0.23)
        self.assertEqual(result["perturbative_correction_hartree"], -0.03)
        self.runner.ccsd_t.assert_called_once_with()

    def test_open_shell_uses_uhf(self):
        result = self.energy("CCSD(T)", spin=3)
        self.scf.UHF.assert_called_once()
        self.scf.RHF.assert_not_called()
        self.assertEqual(result["reference_type"], "UHF")

    def test_unconverged_ccsdt_is_rejected(self):
        self.runner.converged = False
        with self.assertRaisesRegex(core.CalculationError, "CCSDT did not converge"):
            self.energy("CCSDT")

    def test_unconverged_scf_does_not_start_cc(self):
        self.mf.converged = False
        with self.assertRaisesRegex(core.CalculationError, "SCF did not converge"):
            self.energy("CCSD")
        self.cc.CCSD.assert_not_called()

    def test_q_is_rejected_before_scf_when_unavailable(self):
        capability = {"methods": [{"name": "CCSDT(Q)", "closed_shell": False, "open_shell": False, "reason": "No implemented perturbative quadruples"}]}
        with mock.patch("Energyhub.capabilities.get_capabilities", return_value=capability):
            with self.assertRaisesRegex(core.CalculationError, "No implemented perturbative quadruples"):
                self.energy("CCSDT(Q)")
        self.scf.RHF.assert_not_called()
        self.cc.CCSDTQ.assert_not_called()

    def test_q_uses_only_real_ccsdt_q_amplitudes_api(self):
        capability = {"methods": [{"name": "CCSDT(Q)", "closed_shell": True, "open_shell": True, "reason": None}]}
        with mock.patch("Energyhub.capabilities.get_capabilities", return_value=capability):
            result = self.energy("CCSDT(Q)")
        self.assertAlmostEqual(result["energy_hartree"], -10.204)
        self.assertEqual(result["perturbative_correction_hartree"], -0.004)
        self.assertEqual(result["quadruples_bracket_hartree"], -0.003)
        self.runner.ccsdt_q.assert_called_once_with(self.runner.tamps)
        self.runner.ccsd_t.assert_not_called()
        self.cc.CCSDTQ.assert_not_called()

    def test_q_invalid_return_contract_is_rejected(self):
        capability = {"methods": [{"name": "CCSDT(Q)", "closed_shell": True, "open_shell": False, "reason": None}]}
        for value in (-0.004, (-0.003,), (-0.003, float("nan"))):
            with self.subTest(value=value), mock.patch("Energyhub.capabilities.get_capabilities", return_value=capability):
                self.runner.ccsdt_q.return_value = value
                with self.assertRaises(core.CalculationError):
                    self.energy("CCSDT(Q)")

    def test_q_partial_support_rejects_open_shell_before_scf(self):
        capability = {"methods": [{"name": "CCSDT(Q)", "closed_shell": True, "open_shell": False, "reason": None}]}
        with mock.patch("Energyhub.capabilities.get_capabilities", return_value=capability):
            with self.assertRaisesRegex(core.CalculationError, "unavailable for UHF"):
                self.energy("CCSDT(Q)", spin=3)
        self.scf.UHF.assert_not_called()
        self.cc.CCSDT.assert_not_called()

    def test_q_stub_never_falls_back(self):
        capability = {"methods": [{"name": "CCSDT(Q)", "closed_shell": True, "open_shell": True, "reason": None}]}
        self.runner.ccsdt_q.side_effect = NotImplementedError
        with mock.patch("Energyhub.capabilities.get_capabilities", return_value=capability):
            with self.assertRaisesRegex(core.CalculationError, "perturbative CCSDT"):
                self.energy("CCSDT(Q)")
        self.runner.ccsd_t.assert_not_called()
        self.cc.CCSDTQ.assert_not_called()


class CapabilityTests(unittest.TestCase):
    def test_callable_stub_is_unavailable(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / "cc.py"
            source.write_text('class RCCSDT:\n    def ccsdt_q(self, tamps):\n        """Placeholder."""\n        raise NotImplementedError\n')
            self.assertFalse(_implemented_method(source, "RCCSDT", "ccsdt_q"))
            source.write_text("class RCCSDT:\n    def ccsdt_q(self, tamps):\n        return sum(tamps)\n")
            self.assertTrue(_implemented_method(source, "RCCSDT", "ccsdt_q"))


@unittest.skipUnless(importlib.util.find_spec("pyscf"), "optional PySCF environment is unavailable")
class RealPySCFTests(unittest.TestCase):
    def test_h2_ccsd_matches_known_energy(self):
        result = core._cc_energy(molecule(), "CCSD", "cc-pvtz", 512, 1, None)
        self.assertAlmostEqual(result["energy_hartree"], -1.08013772, places=7)
        self.assertAlmostEqual(result["energy_hartree"], result["hf_hartree"] + result["correlation_hartree"], places=12)
        self.assertTrue(result["converged"])

    def test_lih_triples_are_nonzero_and_added_once(self):
        lih = core.Molecule("LiH", Path("LiH.xyz"), 0, 1, "Li 0 0 0\nH 0 0 1.6", 2)
        cc = core._cc_energy(lih, "CCSD", "cc-pvdz", 512, 1, None)
        triples = core._cc_energy(lih, "CCSD(T)", "cc-pvdz", 512, 1, None)
        correction = triples["perturbative_correction_hartree"]
        self.assertLess(correction, -1e-8)
        self.assertAlmostEqual(triples["energy_hartree"] - cc["energy_hartree"], correction, places=10)

    def test_be_quadruples_are_nonzero_and_added_once(self):
        q = next(row for row in get_capabilities()["methods"] if row["name"] == "CCSDT(Q)")
        if not q["closed_shell"]:
            self.skipTest("Installed PySCF does not implement RHF-CCSDT(Q)")
        be = core.Molecule("Be", Path("Be.xyz"), 0, 1, "Be 0 0 0", 1)
        triple = core._cc_energy(be, "CCSDT", "cc-pvdz", 512, 1, None)
        quadruple = core._cc_energy(be, "CCSDT(Q)", "cc-pvdz", 512, 1, None)
        correction = quadruple["perturbative_correction_hartree"]
        self.assertLess(correction, -1e-9)
        self.assertAlmostEqual(quadruple["energy_hartree"] - triple["energy_hartree"], correction, places=10)
        self.assertGreater(abs(correction - quadruple["quadruples_bracket_hartree"]), 1e-10)

    def test_installed_quadruples_status_matches_source(self):
        capabilities = get_capabilities()
        if capabilities["pyscf_version"] == "2.14.0":
            q = next(row for row in capabilities["methods"] if row["name"] == "CCSDT(Q)")
            self.assertTrue(q["available"])
            self.assertTrue(q["closed_shell"])
            self.assertFalse(q["open_shell"])
        elif capabilities["pyscf_version"] == "2.13.1":
            q = next(row for row in capabilities["methods"] if row["name"] == "CCSDT(Q)")
            self.assertFalse(q["available"])
            self.assertIn("NotImplementedError", q["reason"])


if __name__ == "__main__":
    unittest.main()
