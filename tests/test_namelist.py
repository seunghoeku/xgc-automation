from __future__ import annotations

import unittest

from tools import namelist


class ProvenanceInputSummaryTests(unittest.TestCase):
    def test_records_important_parameters_and_derived_physics(self) -> None:
        parsed = {
            "sml_param": {
                "sml_node_file": "'tcv.node'",
                "sml_electron_on": ".true.",
                "sml_electromagnetic": ".true.",
            },
            "eq_param": {"eq_filename": "'tcv.eqd'"},
            "ptl_param": {
                "ptl_marker_type": "'ReducedDeltaF' 'ReducedDeltaF'"
            },
        }

        summary = namelist.provenance_input_summary(parsed)

        self.assertEqual(summary["parse_status"], "ok")
        self.assertEqual(summary["parameters"]["sml_node_file"], "tcv.node")
        self.assertEqual(summary["parameters"]["eq_filename"], "tcv.eqd")
        self.assertIs(summary["parameters"]["sml_electron_on"], True)
        self.assertEqual(
            summary["parameters"]["ptl_marker_type"],
            ["ReducedDeltaF", "ReducedDeltaF"],
        )
        self.assertEqual(summary["derived"]["field_model"], "electromagnetic")
        self.assertEqual(summary["derived"]["marker_model"], "reduced-delta-f")
        self.assertEqual(summary["derived"]["species"], ["e", "i"])

    def test_electron_off_means_ion_only(self) -> None:
        parsed = {
            "sml_param": {
                "sml_electron_on": ".false.",
                "sml_electromagnetic": ".false.",
            },
            "ptl_param": {"ptl_marker_type": "'TotalF'"},
        }

        summary = namelist.provenance_input_summary(parsed)

        self.assertEqual(summary["derived"]["species"], ["i"])
        self.assertEqual(summary["derived"]["field_model"], "electrostatic")
        self.assertEqual(summary["derived"]["marker_model"], "total-f")

    def test_undeclared_legacy_physics_stays_unknown(self) -> None:
        summary = namelist.provenance_input_summary(
            {"sml_param": {"sml_node_file": "'old.node'"}}
        )

        self.assertEqual(summary["derived"]["field_model"], "unknown")
        self.assertEqual(summary["derived"]["marker_model"], "unknown")
        self.assertIsNone(summary["derived"]["species"])
        self.assertIsNone(summary["parameters"]["ptl_marker_type"])

    def test_full_f_is_distinct_from_total_f(self) -> None:
        summary = namelist.provenance_input_summary(
            {"ptl_param": {"ptl_marker_type": ["FullF", "FullF"]}}
        )
        self.assertEqual(summary["derived"]["marker_model"], "full-f")


if __name__ == "__main__":
    unittest.main()
