"""Reference-file contract tests; no PySCF calculation is required."""

import math
import unittest

from Energyhub.core import (
    HARTREE_TO_KCAL,
    CalculationError,
    _RefLine,
    _render_reference,
    parse_reference,
)


class ReferenceTests(unittest.TestCase):
    def render(self, text, energies=None):
        energies = energies or {"A": -1.0, "B": -0.25, "P1": -1.125}
        return _render_reference(parse_reference(text, energies), energies)

    def test_legacy_padded_rows_keep_reference_units(self):
        lines = parse_reference("1\tA\t-1\tB\t\t\t-0.25\n", {"A", "B"})
        self.assertIsNone(lines[0].ratio)
        self.assertEqual(_render_reference(lines, {"A": -2.0, "B": -1.0}), "1 A -1 B -1.000000000000\n")
        lines = parse_reference("1\tA\t-1\tB\t\t\t-100.0\t2\n", {"A", "B"})
        self.assertEqual(lines[0].ratio, 2)
        self.assertEqual(float(_render_reference(lines, {"A": -2.0, "B": -1.0}).split()[-2]), -627.509)

    def test_darc_three_molecule_row_and_ratio_not_applied_twice(self):
        output = self.render("-1 A -1 B 1 P1 ? 2")
        parts = output.split()
        self.assertEqual(parts[:6], ["-1", "A", "-1", "B", "1", "P1"])
        self.assertAlmostEqual(float(parts[6]), 0.125 * HARTREE_TO_KCAL)
        self.assertEqual(parts[7], "2")
        parsed = parse_reference(output, {"A", "B", "P1"})[0]
        self.assertEqual(parsed.pairs, ((-1, "A"), (-1, "B"), (1, "P1")))
        # The downstream DFThub parser normalizes both calc and ref by ratio.
        self.assertAlmostEqual(parsed.ref_value / parsed.ratio, 0.125 * HARTREE_TO_KCAL / 2)

    def test_existing_hartree_reference_one_is_not_a_ratio(self):
        parsed = parse_reference("1 A -1 B 1", {"A", "B"})[0]
        self.assertEqual(parsed.ref_value, 1.0)
        self.assertIsNone(parsed.ratio)
        self.assertEqual(self.render("1 A -1 B 1"), "1 A -1 B -0.750000000000\n")

    def test_existing_kcal_reference_with_fractional_ratio(self):
        output = self.render("1 A -1 B 400 0.5").split()
        self.assertEqual(output[-1], "0.5")
        self.assertAlmostEqual(float(output[-2]), -0.75 * HARTREE_TO_KCAL)

    def test_missing_value_placeholders_keep_units(self):
        for placeholder in ("?", "NA", "na", "null", "NULL"):
            with self.subTest(placeholder=placeholder):
                self.assertEqual(self.render(f"1 A -1 B {placeholder}"), "1 A -1 B -0.750000000000\n")
                output = self.render(f"1 A -1 B {placeholder} 1").split()
                self.assertAlmostEqual(float(output[-2]), -0.75 * HARTREE_TO_KCAL)
                self.assertEqual(output[-1], "1")

    def test_explicit_empty_tab_field_with_and_without_ratio(self):
        self.assertEqual(self.render("1\tA\t-1\tB\t"), "1 A -1 B -0.750000000000\n")
        output = self.render("1\tA\t-1\tB\t\t2").split()
        self.assertAlmostEqual(float(output[-2]), -0.75 * HARTREE_TO_KCAL)
        self.assertEqual(output[-1], "2")
        output = self.render("-1\tA\t-1\tB\t1\tP1\t\t1").split()
        self.assertEqual(output[:6], ["-1", "A", "-1", "B", "1", "P1"])
        self.assertAlmostEqual(float(output[-2]), 0.125 * HARTREE_TO_KCAL)

    def test_omitted_value_after_pairs_means_hartree(self):
        self.assertEqual(self.render("1 A -1 B"), "1 A -1 B -0.750000000000\n")
        self.assertEqual(self.render("1 A"), "1 A -1.000000000000\n")

    def test_numeric_molecule_names(self):
        energies = {"1": -1.0, "2": -0.25}
        for template in ("1 1 -1 2 ?", "1 1 -1 2 -5"):
            self.assertEqual(self.render(template, energies), "1 1 -1 2 -0.750000000000\n")
        output = self.render("1 1 -1 2 ? 1", energies).split()
        self.assertEqual(output[:4], ["1", "1", "-1", "2"])
        self.assertAlmostEqual(float(output[-2]), -0.75 * HARTREE_TO_KCAL)
        self.assertEqual(self.render("1 1", energies), "1 1 -1.000000000000\n")

    def test_comments_blank_lines_and_row_order(self):
        text = "# first reaction\n1 A ? # inline\n\n# second reaction\n1 B ?\n"
        self.assertEqual(
            self.render(text),
            "# first reaction\n1 A -1.000000000000 # inline\n\n# second reaction\n1 B -0.250000000000\n",
        )

    def test_empty_template_keeps_existing_generated_row_contract(self):
        self.assertEqual(parse_reference(""), [])
        self.assertEqual(parse_reference("\n# no rows\n"), [])
        line = _RefLine("1 A", ("1", "A"), ((1.0, "A"),), (), None, 1.0)
        output = _render_reference([line], {"A": -1.0}).split()
        self.assertAlmostEqual(float(output[-2]), -HARTREE_TO_KCAL)
        self.assertEqual(output[-1], "1")

    def test_malformed_rows_are_not_silently_ignored(self):
        for row in ("garbage", "bad A ?", "1 A bad", "1 A ? bad", "1 A ? ?", "1\t\tA 1"):
            with self.subTest(row=row):
                with self.assertRaisesRegex(CalculationError, "Reference line 2"):
                    parse_reference("# heading\n" + row, {"A", "B"})

    def test_unknown_molecules_are_rejected(self):
        for row in ("1 Missing ?", "1 A -1 Missing ?", "1 A -1 B 1 Missing ? 1"):
            with self.subTest(row=row), self.assertRaisesRegex(CalculationError, "not found in archive"):
                parse_reference(row, {"A", "B"})
        with self.assertRaisesRegex(CalculationError, "not found in archive"):
            _render_reference(parse_reference("1 Missing ?"), {"A": -1})

    def test_nonfinite_and_invalid_numeric_values(self):
        for row in (
            "nan A ?", "inf A ?", "1 A nan", "1 A inf", "1 A -inf",
            "1 A ? nan", "1 A ? inf", "1 A ? 0", "1 A ? -1",
            "1\tA\t\t0", "1\tA\t\t-1", "1\tA\t\tnan",
        ):
            with self.subTest(row=row), self.assertRaises(CalculationError):
                parse_reference(row, {"A"})
        for energy in (math.nan, math.inf, -math.inf, "nonnumeric"):
            with self.subTest(energy=energy), self.assertRaises(CalculationError):
                _render_reference(parse_reference("1 A ?"), {"A": energy})


if __name__ == "__main__":
    unittest.main()
