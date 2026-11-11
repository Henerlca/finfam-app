from __future__ import annotations

from calendar import monthrange
from datetime import date
from math import isfinite
from typing import Any, Iterable

from app.services.finance_core import as_date, month_label


CHART_WIDTH = 960
CHART_HEIGHT = 330
LEFT, RIGHT, TOP, BOTTOM = 170.0, 24.0, 20.0, 52.0


def _short_brl(value: float) -> str:
    if abs(value) >= 1_000_000:
        number = f"{value / 1_000_000:.1f}".replace(".", ",")
        return f"R$ {number} mi"
    if abs(value) >= 1_000:
        number = f"{value / 1_000:.1f}".replace(".", ",")
        return f"R$ {number} mil"
    return f"R$ {value:.0f}"


def _full_brl(value: float) -> str:
    """Usar os mesmos centavos das tabelas; não mudar arredondamento financeiro."""
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if formatted.startswith("-"):
        return f"-R$ {formatted[1:]}"
    return f"R$ {formatted}"


def _label(row: dict[str, Any]) -> str:
    if row.get("label"):
        return str(row["label"])
    return month_label(row.get("month"))


def _tooltip_label(row: dict[str, Any]) -> str:
    if row.get("tooltip_label"):
        return str(row["tooltip_label"])
    return _label(row)


def _tooltip_status(field: str, row: dict[str, Any]) -> str:
    normalized = str(field).strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized == "confirmed":
        return "Confirmado"
    if normalized == "minimum_balance" or "piso" in normalized or "floor" in normalized:
        return "Referência"
    if normalized == "projected":
        return "Projeção"
    if bool(row.get("closed", False)):
        return "Mês fechado"
    return "Projeção"


def build_line_chart(
    rows: Iterable[dict[str, Any]],
    series_specs: list[tuple[str, str, str, bool]],
    chart_id: str,
    aria_label: str,
    max_x_labels: int = 7,
) -> dict[str, Any] | None:
    """Build accessible SVG coordinates from already-computed values.

    Each series spec is (field, label, color, dashed). No finance calculations
    are performed here; this function only maps values to SVG geometry.
    """
    data = list(rows)
    if not data or not series_specs:
        return None

    valid_specs: list[tuple[str, str, str, bool]] = []
    all_values: list[float] = []
    for field, label, color, dashed in series_specs:
        values = []
        for row in data:
            raw = row.get(field)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if isfinite(value):
                values.append(value)
        if values:
            valid_specs.append((field, label, color, dashed))
            all_values.extend(values)
    if not all_values:
        return None

    y_min, y_max = min(all_values), max(all_values)
    span = y_max - y_min
    if span < 0.01:
        span = max(abs(y_max) * 0.1, 100.0)
        y_min -= span / 2
        y_max += span / 2
    else:
        pad = span * 0.08
        y_min -= pad
        y_max += pad
    if y_min == y_max:
        y_min -= 1
        y_max += 1

    plot_width = CHART_WIDTH - LEFT - RIGHT
    plot_height = CHART_HEIGHT - TOP - BOTTOM

    def x_at(index: int) -> float:
        if len(data) == 1:
            return LEFT + plot_width / 2
        return LEFT + plot_width * index / (len(data) - 1)

    def y_at(value: float) -> float:
        return TOP + plot_height * (y_max - value) / (y_max - y_min)

    ticks = []
    for index in range(5):
        value = y_max - (y_max - y_min) * index / 4
        ticks.append({"y": round(y_at(value), 2), "label": _short_brl(value)})

    label_count = min(max(1, max_x_labels), len(data))
    if label_count == 1:
        label_indices = {0}
    else:
        label_indices = {
            round(index * (len(data) - 1) / (label_count - 1))
            for index in range(label_count)
        }
    x_labels = [
        {"x": round(x_at(index), 2), "label": _label(data[index]),
         "anchor": "start" if index == 0 else "end" if index == len(data) - 1 else "middle"}
        for index in sorted(label_indices)
    ]

    series = []
    for field, label, color, dashed in valid_specs:
        points = []
        for index, row in enumerate(data):
            raw = row.get(field)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if not isfinite(value):
                continue
            tooltip_text = _tooltip_label(row)
            status = _tooltip_status(field, row)
            points.append({
                "index": index,
                "x": round(x_at(index), 2),
                "y": round(y_at(value), 2),
                "value": value,
                "title": f"{tooltip_text} · {label}: {_full_brl(value)} · {status}",
                "closed": bool(row.get("closed", False)),
            })
        if len(points) < 1:
            continue
        path = " ".join(("M" if index == 0 else "L") + f"{point['x']},{point['y']}" for index, point in enumerate(points))
        series.append({"label": label, "color": color, "dashed": dashed, "path": path, "points": points})

    tooltip_points = []
    for index, row in enumerate(data):
        entries = []
        for field, label, color, dashed in valid_specs:
            raw = row.get(field)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if not isfinite(value):
                continue
            entries.append({
                "label": label,
                "color": color,
                "value": _full_brl(value),
                "status": _tooltip_status(field, row),
            })
        if entries:
            tooltip_points.append({
                "index": index,
                "x": round(x_at(index), 2),
                "label": _tooltip_label(row),
                "entries": entries,
            })

    return {
        "id": chart_id,
        "view_box": f"0 0 {CHART_WIDTH} {CHART_HEIGHT}",
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "plot": {"left": LEFT, "right": CHART_WIDTH - RIGHT, "top": TOP, "bottom": CHART_HEIGHT - BOTTOM},
        "ticks": ticks,
        "x_labels": x_labels,
        "series": series,
        "tooltip_points": tooltip_points,
        "aria_label": aria_label,
    }


def build_stacked_bar_chart(
    rows: Iterable[dict[str, Any]], series_specs: list[tuple[str, str, str]],
    chart_id: str, aria_label: str, max_x_labels: int = 5,
) -> dict[str, Any] | None:
    """Build cumulative-category bars; values are read-only and never recalculated financially."""
    data = list(rows)
    specs = [(field, label, color) for field, label, color in series_specs]
    if not data or not specs:
        return None
    stacked_values = []
    for row in data:
        positive = negative = 0.0
        for field, _, _ in specs:
            try:
                value = float(row.get(field) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if not isfinite(value):
                value = 0.0
            if value >= 0:
                positive += value
            else:
                negative += value
        stacked_values.extend((positive, negative))
    y_min, y_max = min(0.0, *stacked_values), max(0.0, *stacked_values)
    if y_min == y_max:
        y_min, y_max = -1.0, 1.0
    else:
        padding = (y_max - y_min) * 0.06
        y_min -= padding
        y_max += padding

    plot_width = CHART_WIDTH - LEFT - RIGHT
    plot_height = CHART_HEIGHT - TOP - BOTTOM
    x_at = lambda index: LEFT + plot_width / 2 if len(data) == 1 else LEFT + plot_width * index / (len(data) - 1)
    y_at = lambda value: TOP + plot_height * (y_max - value) / (y_max - y_min)
    ticks = []
    for index in range(5):
        value = y_max - (y_max - y_min) * index / 4
        ticks.append({"y": round(y_at(value), 2), "label": _short_brl(value)})

    label_count = min(max(1, max_x_labels), len(data))
    label_indices = {0} if label_count == 1 else {
        round(index * (len(data) - 1) / (label_count - 1)) for index in range(label_count)
    }
    x_labels = [{"x": round(x_at(index), 2), "label": _label(data[index]),
                 "anchor": "start" if index == 0 else "end" if index == len(data) - 1 else "middle"}
                for index in sorted(label_indices)]
    slot = plot_width / max(1, len(data))
    bar_width = min(52.0, max(5.0, slot * 0.64))
    bars, tooltip_points = [], []
    for index, row in enumerate(data):
        positive = negative = 0.0
        segments, entries = [], []
        status = "Mês fechado" if row.get("closed") else "Projeção"
        for field, label, color in specs:
            try:
                value = float(row.get(field) or 0)
            except (TypeError, ValueError):
                continue
            if not isfinite(value):
                continue
            if abs(value) >= 0.005:
                entries.append({"label": label, "color": color, "value": _full_brl(value), "status": status})
            if value >= 0:
                start, end = positive, positive + value
                positive = end
            else:
                start, end = negative, negative + value
                negative = end
            if abs(value) < 0.005:
                continue
            y1, y2 = y_at(start), y_at(end)
            tooltip_label = _tooltip_label(row)
            segments.append({
                "x": round(x_at(index) - bar_width / 2, 2), "y": round(min(y1, y2), 2),
                "width": round(bar_width, 2), "height": round(max(1.0, abs(y2 - y1)), 2),
                "color": color, "index": index,
                "title": f"{tooltip_label} · {label}: {_full_brl(value)} · {status}",
            })
        try:
            total = float(row.get("projected", positive + negative))
        except (TypeError, ValueError):
            total = positive + negative
        entries.append({"label": "Total das caixinhas", "color": "#123b63", "value": _full_brl(total), "status": status})
        bars.append({"index": index, "segments": segments})
        tooltip_points.append({"index": index, "x": round(x_at(index), 2), "label": _tooltip_label(row), "entries": entries})

    return {
        "id": chart_id, "kind": "stacked_bar", "view_box": f"0 0 {CHART_WIDTH} {CHART_HEIGHT}",
        "width": CHART_WIDTH, "height": CHART_HEIGHT,
        "plot": {"left": LEFT, "right": CHART_WIDTH - RIGHT, "top": TOP, "bottom": CHART_HEIGHT - BOTTOM},
        "zero_y": round(y_at(0.0), 2), "ticks": ticks, "x_labels": x_labels, "bars": bars,
        "series": [{"label": label, "color": color, "dashed": False} for _, label, color in specs],
        "tooltip_points": tooltip_points, "aria_label": aria_label,
    }


def build_daily_cash_chart(items: Iterable[dict[str, Any]], opening: float, month: Any) -> dict[str, Any]:
    """Build confirmed and scheduled daily cash lines from calendar dates."""
    selected = as_date(month)
    if selected is None:
        return {"chart": None}
    days = monthrange(selected.year, selected.month)[1]
    confirmed_delta = [0.0] * (days + 1)
    projected_delta = [0.0] * (days + 1)

    for item in items:
        amount = float(item.get("signed_amount") or 0)
        precision = str(item.get("date_precision") or "exact")
        scheduled = as_date(item.get("transaction_date"))
        paid = bool(item.get("paid"))
        paid_date = as_date(item.get("paid_on")) if paid else None
        projected_date = paid_date or scheduled
        if projected_date is None or (projected_date.year, projected_date.month) != (selected.year, selected.month):
            continue
        projected_delta[projected_date.day] += amount
        if paid:
            actual_date = paid_date or (scheduled if precision == "exact" else None)
            if actual_date and (actual_date.year, actual_date.month) == (selected.year, selected.month):
                confirmed_delta[actual_date.day] += amount

    first_day = selected.replace(day=1)
    data = [{
        "label": "Início",
        "tooltip_label": f"Início de {first_day:%d/%m/%Y}",
        "confirmed": float(opening),
        "projected": float(opening),
    }]
    confirmed, projected = float(opening), float(opening)
    for day in range(1, days + 1):
        confirmed += confirmed_delta[day]
        projected += projected_delta[day]
        data.append({
            "label": f"{day:02d}",
            "tooltip_label": f"{day:02d}/{selected.month:02d}/{selected.year:04d}",
            "confirmed": confirmed,
            "projected": projected,
        })

    chart = build_line_chart(
        data,
        [
            ("confirmed", "Baixas confirmadas", "#123b63", False),
            ("projected", "Com previsões", "#79a7d0", True),
        ],
        "cash-daily",
        "Saldo pelas baixas e projeção diária do caixa",
        max_x_labels=7,
    )
    return {"chart": chart}


def classify_pot(name: str) -> str:
    normalized = str(name or "").casefold()
    if any(word in normalized for word in ("gasolina", "combustível", "combustivel", "auto", "carro", "veículo", "veiculo", "mercado", "aluguel", "saúde", "saude", "remédio", "remedio", "transporte", "necessidade")):
        return "Essenciais"
    if any(word in normalized for word in ("lazer", "viagem", "presentes", "presente", "hobby", "entretenimento", "roupa", "vestuário", "vestuario", "luxo", "passeio")):
        return "Lazer e escolhas"
    return "Outros objetivos"


def group_pots(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {"Essenciais": [], "Lazer e escolhas": [], "Outros objetivos": []}
    for original in rows:
        row = dict(original)
        group = classify_pot(row.get("pot", ""))
        reserved = max(0.0, float(row.get("reserved") or 0))
        used = max(0.0, float(row.get("used") or 0))
        row["group"] = group
        row["used_percent"] = min(100.0, (used / reserved * 100) if reserved else (100.0 if used else 0.0))
        row["display_name"] = str(row.get("pot", "")).removeprefix("Reserva - ")
        groups[group].append(row)
    return [{"name": name, "rows": values} for name, values in groups.items() if values]


WATERFALL_COLORS = {
    "total": "#123b63",
    "income": "#2d6f9f",
    "transfer": "#7185a8",
    "expense": "#b85f5b",
    "reserve": "#c08a39",
    "savings": "#6e78ae",
}
CATEGORY_SERIES = (
    ("planned", "Planejado/lançado", "#3974a3"),
    ("confirmed", "Confirmado no app", "#1e5d78"),
    ("previous", "Confirmado no mês anterior", "#9aa9b7"),
)


def build_waterfall_chart(steps: Iterable[dict[str, Any]], chart_id: str, aria_label: str) -> dict[str, Any] | None:
    """Map signed monthly movements to SVG waterfall geometry, without finance logic."""
    rows = [dict(row) for row in steps]
    if len(rows) < 2:
        return None
    width, height = 1060, 390
    left, right, top, bottom = 54.0, 22.0, 22.0, 82.0
    plot_width, plot_height = width - left - right, height - top - bottom
    running = 0.0
    prepared = []
    values = [0.0]
    for index, row in enumerate(rows):
        try:
            value = round(float(row.get("value") or 0), 2)
        except (TypeError, ValueError):
            value = 0.0
        is_total = row.get("kind") == "total"
        start_value = 0.0 if is_total else running
        end_value = value if is_total else round(running + value, 2)
        if not is_total:
            running = end_value
        elif index == 0:
            running = end_value
        values.extend((start_value, end_value))
        prepared.append({**row, "value": value, "start": start_value, "end": end_value, "is_total": is_total})

    y_min, y_max = min(values), max(values)
    span = y_max - y_min
    pad = max(span * 0.08, 100.0)
    y_min -= pad
    y_max += pad
    if y_max <= y_min:
        y_max = y_min + 1.0

    def y_at(value: float) -> float:
        return top + plot_height * (y_max - value) / (y_max - y_min)

    ticks = []
    for i in range(5):
        value = y_max - (y_max - y_min) * i / 4
        ticks.append({"y": round(y_at(value), 2), "label": _short_brl(value)})
    slot = plot_width / len(prepared)
    bar_width = min(55.0, slot * 0.62)
    bars = []
    for index, row in enumerate(prepared):
        x = left + slot * index + (slot - bar_width) / 2
        upper, lower = max(row["start"], row["end"]), min(row["start"], row["end"])
        y = y_at(upper)
        bar_height = max(2.0, y_at(lower) - y)
        color = WATERFALL_COLORS.get(str(row.get("kind") or "expense"), WATERFALL_COLORS["expense"])
        title_value = row["value"] if not row["is_total"] else row["end"]
        confirmation = float(row.get("confirmed_value") or 0)
        if row["value"] >= 0:
            confirmation = max(0.0, min(row["value"], confirmation))
        else:
            confirmation = min(0.0, max(row["value"], confirmation))
        confirmed_end = round(row["start"] + confirmation, 2)
        confirmed_upper, confirmed_lower = max(row["start"], confirmed_end), min(row["start"], confirmed_end)
        confirmed_y = y_at(confirmed_upper)
        confirmed_height = max(0.0, y_at(confirmed_lower) - confirmed_y) if abs(confirmation) >= 0.005 else 0.0
        if confirmed_height < 0.5:
            confirmed_height = 0.0
        forecast_opacity = 0.28 if not row["is_total"] and abs(confirmation) < abs(row["value"]) - 0.005 else 1.0
        title = f"{row.get('label', '')} · {'Saldo' if row['is_total'] else 'Movimento'}: {_full_brl(title_value)}"
        if not row["is_total"]:
            title += f" · Confirmado no app: {_full_brl(abs(confirmation))} · {row.get('status', 'Previsto')} · Saldo após etapa: {_full_brl(row['end'])}"
        words = str(row.get("label") or "").split()
        label_lines = []
        line = ""
        for word in words:
            if line and len(line) + len(word) + 1 > 11:
                label_lines.append(line)
                line = word
            else:
                line = (line + " " + word).strip()
        if line:
            label_lines.append(line)
        if len(label_lines) > 2:
            label_lines = [label_lines[0], label_lines[1][:9] + "…"]
        bars.append({
            "x": round(x, 2), "y": round(y, 2), "width": round(bar_width, 2),
            "height": round(bar_height, 2), "color": color, "label": row.get("label", ""),
            "label_lines": label_lines, "title": title, "value": row["value"],
            "confirmed_value": confirmation, "confirmed_y": round(confirmed_y, 2),
            "confirmed_height": round(confirmed_height, 2), "forecast_opacity": forecast_opacity,
            "status": row.get("status", "Previsto"),
            "end": row["end"], "total": row["is_total"],
        })
    closing = next((row["end"] for row in reversed(prepared) if row["is_total"]), running)
    reconciled = abs(round(running - closing, 2)) <= 0.005
    connectors = [
        {"x1": round(bars[index]["x"] + bars[index]["width"], 2), "x2": round(bars[index + 1]["x"], 2),
         "y": round(y_at(prepared[index]["end"]), 2)}
        for index in range(len(bars) - 1)
    ]
    legend = [
        {"label": label, "color": color}
        for label, color in (("Saldo inicial/final", WATERFALL_COLORS["total"]), ("Receitas", WATERFALL_COLORS["income"]),
                             ("Despesas", WATERFALL_COLORS["expense"]), ("Transferências", WATERFALL_COLORS["transfer"]),
                             ("Reservas", WATERFALL_COLORS["reserve"]), ("Caixinhas", WATERFALL_COLORS["savings"]))
    ]
    legend.extend((
        {"label": "Confirmado", "color": WATERFALL_COLORS["income"], "opacity": "1"},
        {"label": "Previsto", "color": WATERFALL_COLORS["income"], "opacity": "0.28"},
    ))
    return {
        "id": chart_id, "aria_label": aria_label, "view_box": f"0 0 {width} {height}",
        "width": width, "height": height, "plot": {"left": left, "right": width - right, "top": top, "bottom": height - bottom},
        "ticks": ticks, "bars": bars, "connectors": connectors, "reconciled": reconciled,
        "running_end": round(running, 2), "closing": round(closing, 2),
        "legend": legend,
    }


def build_category_chart(rows: Iterable[dict[str, Any]], chart_id: str = "category-spend", aria_label: str = "Despesas por categoria: planejadas, confirmadas e mês anterior", max_categories: int = 8) -> dict[str, Any] | None:
    """Build grouped horizontal SVG bars from pre-aggregated category values."""
    source = [dict(row) for row in rows]
    if not source:
        return None
    limit = max(1, int(max_categories))
    if len(source) > limit:
        kept = source[:limit - 1]
        rest = source[limit - 1:]
        other: dict[str, Any] = {key: round(sum(float(row.get(key) or 0) for row in rest), 2) for key in ("planned", "confirmed", "previous")}
        other["category"] = "Outras categorias"
        other["change"] = round(other["confirmed"] - other["previous"], 2)
        other["change_percent"] = (other["change"] / other["previous"] * 100) if other["previous"] else None
        source = kept + [other]
    maximum = max((float(row.get(field) or 0) for row in source for field, _, _ in CATEGORY_SERIES), default=0.0)
    if maximum <= 0:
        maximum = 1.0
    width, top, left, right, row_height = 1060, 42.0, 205.0, 90.0, 72.0
    height = int(top + len(source) * row_height + 22)
    plot_width = width - left - right
    plot_right = width - right
    grid = []
    for i in range(5):
        value = maximum * i / 4
        x = left + plot_width * i / 4
        grid.append({"x": round(x, 2), "label": _short_brl(value)})
    bars = []
    for row_index, row in enumerate(source):
        group_top = top + row_index * row_height
        category = str(row.get("category") or "Sem categoria")
        for series_index, (field, series_name, color) in enumerate(CATEGORY_SERIES):
            value = max(0.0, float(row.get(field) or 0))
            bar_width = plot_width * value / maximum
            y = group_top + 5 + series_index * 19
            bars.append({
                "x": left, "y": round(y, 2), "width": round(bar_width, 2), "height": 11,
                "color": color, "series": series_name, "category": category,
                "value": round(value, 2), "value_label": _short_brl(value),
                "title": f"{category} · {series_name}: {_full_brl(value)}",
                "value_x": round(min(plot_right - 2, left + bar_width + 7), 2),
                "value_anchor": "start" if left + bar_width + 54 < plot_right else "end",
            })
    categories = [{"label": str(row.get("category") or "Sem categoria"), "y": round(top + i * row_height + 39, 2)} for i, row in enumerate(source)]
    mobile_categories = []
    for row in source:
        mobile_series = []
        for field, series_name, color in CATEGORY_SERIES:
            value = max(0.0, float(row.get(field) or 0))
            mobile_series.append({
                "label": {"Planejado/lançado": "Planejado", "Confirmado no app": "Confirmado", "Confirmado no mês anterior": "Mês anterior"}.get(series_name, series_name),
                "value": round(value, 2), "width": round(min(100.0, value / maximum * 100), 1), "color": color,
            })
        mobile_categories.append({"category": str(row.get("category") or "Sem categoria"), "series": mobile_series})
    return {
        "id": chart_id, "aria_label": aria_label, "view_box": f"0 0 {width} {height}", "width": width,
        "height": height, "plot": {"left": left, "right": plot_right, "top": top, "bottom": height - 18},
        "grid": grid, "bars": bars, "categories": categories, "mobile_categories": mobile_categories,
        "legend": [{"label": label, "color": color} for _, label, color in CATEGORY_SERIES],
    }
