from __future__ import annotations

from copy import deepcopy
from datetime import date
from math import inf, nan
import unittest

from app.services.charts import build_daily_cash_chart, build_line_chart


class ChartTooltipTests(unittest.TestCase):
    def test_full_currency_signs_cents_thousands_and_millions(self):
        chart = build_line_chart(
            [
                {"label": "Primeiro", "value": 12.3},
                {"label": "Mil", "value": 12_345.67},
                {"label": "Negativo", "value": -1_234.56},
                {"label": "Milhão", "value": 1_234_567.89},
            ],
            [("value", "Saldo", "#123b63", False)],
            "currency",
            "Saldos",
        )
        assert chart is not None
        self.assertEqual(
            [entry["value"] for point in chart["tooltip_points"] for entry in point["entries"]],
            ["R$ 12,30", "R$ 12.345,67", "-R$ 1.234,56", "R$ 1.234.567,89"],
        )
        self.assertIn("R$ 1.234.567,89", chart["series"][0]["points"][-1]["title"])

    def test_currency_uses_existing_table_rounding(self):
        from app.routes import brl
        from app.services.charts import _full_brl

        for value in (2.675, 1.005, -2.675, -0.001, 12345.675, 1000000.015):
            self.assertEqual(_full_brl(value), brl(value))

    def test_missing_nonfinite_values_are_not_zero_tooltip_entries(self):
        chart = build_line_chart(
            [
                {"label": "Ausente", "confirmed": None, "projected": nan},
                {"label": "Só projetado", "confirmed": None, "projected": 5},
                {"label": "Infinito", "confirmed": inf, "projected": -2},
                {"label": "Zero real", "confirmed": 0, "projected": -1},
            ],
            [
                ("confirmed", "Confirmado", "#123b63", False),
                ("projected", "Com previsões", "#79a7d0", True),
            ],
            "sparse",
            "Saldos",
        )
        assert chart is not None
        self.assertEqual([point["index"] for point in chart["tooltip_points"]], [1, 2, 3])
        self.assertEqual([point["index"] for point in chart["series"][0]["points"]], [3])
        self.assertEqual([point["index"] for point in chart["series"][1]["points"]], [1, 2, 3])
        self.assertEqual(len(chart["tooltip_points"][0]["entries"]), 1)
        self.assertEqual(chart["tooltip_points"][0]["entries"][0]["value"], "R$ 5,00")
        self.assertEqual(
            {entry["value"] for entry in chart["tooltip_points"][2]["entries"]},
            {"R$ 0,00", "-R$ 1,00"},
        )

    def test_single_row_keeps_original_index_and_center_x(self):
        chart = build_line_chart(
            [{"label": "Único", "confirmed": 100}],
            [("confirmed", "Baixas confirmadas", "#123b63", False)],
            "single",
            "Saldo",
        )
        assert chart is not None
        self.assertEqual(chart["tooltip_points"][0]["index"], 0)
        self.assertEqual(chart["tooltip_points"][0]["x"], 553.0)
        self.assertEqual(chart["series"][0]["points"][0]["index"], 0)
        self.assertEqual(chart["series"][0]["points"][0]["value"], 100.0)

    def test_overlapping_entries_have_field_statuses_and_closed_status(self):
        rows = [{
            "label": "Abril",
            "confirmed": 100,
            "projected": 110,
            "minimum_balance": 50,
            "floor": 50,
            "other": 25,
            "closed": True,
        }]
        chart = build_line_chart(
            rows,
            [
                ("confirmed", "Confirmado", "#123b63", False),
                ("projected", "Com previsões", "#79a7d0", True),
                ("minimum_balance", "Piso", "#a9433d", True),
                ("floor", "Ref piso", "#a9433d", True),
                ("other", "Outro", "#444444", False),
            ],
            "overlap",
            "Saldos",
        )
        assert chart is not None
        self.assertEqual(
            [entry["status"] for entry in chart["tooltip_points"][0]["entries"]],
            ["Confirmado", "Projeção", "Referência", "Referência", "Mês fechado"],
        )
        self.assertEqual(chart["series"][0]["points"][0]["title"].split(" · ")[-1], "Confirmado")
        self.assertIn("Mês fechado", chart["series"][-1]["points"][0]["title"])

    def test_daily_tooltips_use_full_dates_and_distinguish_confirmed_projection(self):
        items = [
            {
                "signed_amount": -20,
                "amount": 20,
                "date_precision": "exact",
                "transaction_date": "2026-02-02",
                "paid": True,
                "paid_on": "2026-02-02",
            },
            {
                "signed_amount": -10,
                "amount": 10,
                "date_precision": "exact",
                "transaction_date": "2026-02-02",
                "paid": False,
                "paid_on": None,
            },
        ]
        result = build_daily_cash_chart(items, 100, date(2026, 2, 1))
        chart = result["chart"]
        assert chart is not None
        self.assertEqual(chart["x_labels"][0]["label"], "Início")
        self.assertEqual(chart["series"][0]["points"][1]["index"], 1)
        self.assertEqual(chart["series"][0]["points"][1]["title"].split(" · ")[0], "01/02/2026")
        self.assertEqual(chart["tooltip_points"][0]["label"], "Início de 01/02/2026")
        day_two = next(point for point in chart["tooltip_points"] if point["index"] == 2)
        self.assertEqual(day_two["label"], "02/02/2026")
        self.assertEqual([entry["status"] for entry in day_two["entries"]], ["Confirmado", "Projeção"])
        self.assertEqual([entry["value"] for entry in day_two["entries"]], ["R$ 80,00", "R$ 70,00"])
        self.assertEqual(chart["series"][0]["points"][2]["value"], 80.0)
        self.assertEqual(chart["series"][1]["points"][2]["value"], 70.0)

    def test_builders_do_not_mutate_original_inputs(self):
        rows = [{"label": "Jan", "confirmed": 10, "projected": 12}]
        original_rows = deepcopy(rows)
        items = [{
            "signed_amount": -5,
            "amount": 5,
            "date_precision": "exact",
            "transaction_date": "2026-01-03",
            "paid": False,
            "paid_on": None,
        }]
        original_items = deepcopy(items)
        build_line_chart(rows, [("confirmed", "Confirmado", "#123b63", False)], "rows", "Rows")
        build_daily_cash_chart(items, 100, "2026-01")
        self.assertEqual(rows, original_rows)
        self.assertEqual(items, original_items)


if __name__ == "__main__":
    unittest.main()
