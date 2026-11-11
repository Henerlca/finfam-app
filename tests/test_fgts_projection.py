from datetime import date
import unittest

from app.services.fgts_projection import project_fgts
from app.services.finance_core import month_from_id, month_id


SETTINGS = {
    "fgts_salary_hener": 4000,
    "fgts_salary_ju": 3000,
    "fgts_rate": 0.08,
}


def forecast_rows(start: date, end: date):
    start_id = month_id(start)
    end_id = month_id(end)
    assert start_id is not None and end_id is not None
    return [{"month": month_from_id(index), "projected_ending_balance": 1000 + index - start_id}
            for index in range(start_id, end_id + 1)]


class FGTSProjectionTests(unittest.TestCase):
    def test_demo_months_use_configured_bases_without_personal_opening_balance(self):
        rows = forecast_rows(date(2026, 11, 1), date(2027, 1, 1))
        projected = project_fgts(rows, SETTINGS)
        self.assertEqual(round(projected[0]["fgts_monthly"], 2), 0.00)
        self.assertEqual(round(projected[0]["fgts_accumulated"], 2), 0.00)
        self.assertEqual(round(projected[1]["fgts_monthly"], 2), 1120.00)
        self.assertEqual(round(projected[1]["fgts_accumulated"], 2), 1120.00)
        self.assertEqual(round(projected[2]["fgts_monthly"], 2), 560.00)
        self.assertEqual(round(projected[2]["fgts_accumulated"], 2), 1680.00)
        self.assertEqual([row["projected_ending_balance"] for row in projected], [row["projected_ending_balance"] for row in rows])

    def test_december_includes_a_thirteenth_salary_contribution(self):
        rows = forecast_rows(date(2026, 11, 1), date(2026, 12, 1))
        projected = project_fgts(rows, SETTINGS)
        self.assertEqual(round(projected[-1]["fgts_monthly"], 2), 1120.00)

    def test_projection_continues_after_2030_using_demo_bases(self):
        rows = forecast_rows(date(2026, 11, 1), date(2031, 12, 1))
        projected = project_fgts(rows, SETTINGS)
        february_2031 = next(row for row in projected if row["month"] == date(2031, 2, 1))
        december_2031 = projected[-1]
        self.assertEqual(round(february_2031["fgts_monthly"], 2), 560.00)
        self.assertEqual(round(december_2031["fgts_monthly"], 2), 1120.00)
        self.assertGreater(december_2031["fgts_accumulated"], projected[-13]["fgts_accumulated"])

    def test_invalid_assumptions_fail_closed_and_pre_base_values_are_not_invented(self):
        rows = forecast_rows(date(2026, 10, 1), date(2026, 11, 1))
        result = project_fgts(rows, SETTINGS)
        self.assertIsNone(result[0]["fgts_monthly"])
        self.assertIsNone(result[0]["fgts_accumulated"])
        self.assertEqual(result[1]["fgts_monthly"], 0.0)
        with self.assertRaises(ValueError):
            project_fgts(rows, {**SETTINGS, "fgts_salary_ju": -1})


if __name__ == "__main__":
    unittest.main()
