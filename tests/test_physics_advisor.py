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

        self.assertEqual(result["status"], "unsupported")
        self.assertNotIn("selected_case", result)
        excluded = result["candidates"][0]["excluded"]
        self.assertTrue(any("electrostatic" in reason for reason in excluded))
        self.assertTrue(any("core case" in reason for reason in excluded))

    def test_asks_for_detail_when_no_signal_matches(self) -> None:
        result = pa.advise("I want to study a plasma problem.")

        self.assertEqual(result["status"], "needs_clarification")
        self.assertTrue(result["questions"])

    def test_filters_by_machine(self) -> None:
        result = pa.advise(
            "Electrostatic core delta-f turbulence.",
            machine="polaris",
        )

        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["candidates"], [])

    def test_korean_request_is_supported(self) -> None:
        result = pa.advise("무충돌 정전기 코어 ITG 난류를 25단계 새 계산으로 실행")

        self.assertEqual(result["status"], "recommended")
        self.assertEqual(result["proposal"]["overrides"]["sml_mstep"], 25)
        self.assertFalse(result["proposal"]["overrides"]["sml_restart"])


if __name__ == "__main__":
    unittest.main()
