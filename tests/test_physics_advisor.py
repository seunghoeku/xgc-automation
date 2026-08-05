from __future__ import annotations

import unittest

from tools import physics_advisor as pa


class PhysicsAdvisorTests(unittest.TestCase):
    def test_recommends_ecbc_without_inventing_values(self) -> None:
        result = pa.advise(
            "Run a collisionless electrostatic core ITG delta-f simulation."
        )

        self.assertEqual(result["status"], "recommended")
        self.assertEqual(result["selected_case"]["name"], "ecbc")
        self.assertEqual(result["proposal"]["overrides"], {})
        self.assertEqual(result["proposal"]["invented_values"], [])

    def test_carries_only_explicit_step_and_fresh_run_values(self) -> None:
        result = pa.advise(
            "Fresh electrostatic core delta-f run for 50 steps without a restart."
        )

        self.assertEqual(result["status"], "recommended")
        self.assertEqual(
            result["proposal"]["overrides"],
            {"sml_mstep": 50, "sml_restart": False},
        )

    def test_rejects_electromagnetic_edge_request(self) -> None:
        result = pa.advise(
            "Electromagnetic edge-SOL turbulence with collisions enabled."
        )

        # Electromagnetic disqualifies every case in the KB (ecbc is a core
        # electrostatic case; st40-heatload is electrostatic too).
        self.assertEqual(result["status"], "unsupported")
        self.assertNotIn("selected_case", result)
        self.assertTrue(result["candidates"])
        for candidate in result["candidates"]:
            self.assertTrue(candidate["excluded"])
        ecbc = next(c for c in result["candidates"] if c["case"] == "ecbc")
        self.assertTrue(any("electrostatic" in r for r in ecbc["excluded"]))
        self.assertTrue(any("core case" in r for r in ecbc["excluded"]))

    def test_asks_for_detail_when_no_signal_matches(self) -> None:
        result = pa.advise("I want to study a plasma problem.")

        self.assertEqual(result["status"], "needs_clarification")
        self.assertTrue(result["questions"])

    def test_filters_by_machine(self) -> None:
        result = pa.advise(
            "Electrostatic core delta-f turbulence.",
            machine="polaris",
        )

        # ecbc (stellar) is filtered out by the machine constraint, so the
        # perfect match is unavailable. The polaris cases either get excluded
        # (st40-heatload: delta-f) or match too weakly to recommend
        # (asdex-imode: electrostatic only), so no case is recommended.
        self.assertIn(result["status"], {"unsupported", "needs_clarification"})
        names = {c["case"] for c in result["candidates"]}
        self.assertNotIn("ecbc", names)
        recommended = result.get("selected_case")
        if recommended is not None:
            # A weak single-signal match must not be presented as recommended.
            self.assertNotEqual(result["status"], "recommended")

    def test_filters_by_machine_with_no_cases_at_all(self) -> None:
        result = pa.advise(
            "Electrostatic core delta-f turbulence.",
            machine="no-such-machine",
        )

        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["candidates"], [])

    def test_explicit_english_step_and_fresh_run_request_is_supported(self) -> None:
        result = pa.advise(
            "Run a fresh simulation for 25 steps using collisionless "
            "electrostatic core ITG physics."
        )

        self.assertEqual(result["status"], "recommended")
        self.assertEqual(result["proposal"]["overrides"]["sml_mstep"], 25)
        self.assertFalse(result["proposal"]["overrides"]["sml_restart"])

    def test_recommends_tcv_xgca_regular_sheath_case(self) -> None:
        result = pa.advise(
            "TCV 69271 negative-triangularity XGCa regular sheath model"
        )

        self.assertEqual(result["status"], "recommended")
        self.assertEqual(result["selected_case"]["name"], "tcv-xgca-sheath")
        self.assertEqual(result["selected_case"]["machine"], "perlmutter")
        self.assertIn("sml_sheath_mode", result["proposal"]["baseline_parameters"])

    def test_regular_sheath_case_rejects_gyraze_request(self) -> None:
        result = pa.advise("TCV XGCa Gyraze surrogate sheath")

        candidate = next(
            item for item in result["candidates"]
            if item["case"] == "tcv-xgca-sheath"
        )
        self.assertTrue(candidate["excluded"])


if __name__ == "__main__":
    unittest.main()
