"""Teste de UI dos tooltips; sem servidor, banco, login ou dados da família."""
from pathlib import Path
import shutil

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.services.charts import build_line_chart


@pytest.mark.parametrize("width,height", [(1280, 900), (375, 812), (320, 568)])
def test_exact_tooltips_mouse_touch_and_keyboard(width, height):
    playwright = pytest.importorskip("playwright.sync_api")
    browser_path = shutil.which("chromium")
    if not browser_path:
        pytest.skip("Chromium não disponível para o teste de UI isolado")
    root = Path(__file__).resolve().parents[1]
    chart = build_line_chart([
        {"month": "2031-01-01", "base": 12345.67, "other": -890.12},
        {"month": "2031-02-01", "base": 9876.54, "other": 9876.54},
        {"month": "2031-03-01", "base": 1000000.01, "other": None},
    ], [("base", "Saldo <principal>", "#123b63", False),
        ("other", "Alternativa", "#79a7d0", True)], "tooltip-test", "Valores de teste")
    assert chart is not None
    env = Environment(loader=FileSystemLoader(root / "app/templates"), autoescape=select_autoescape())
    markup = getattr(env.get_template("_chart.html").module, "chart_svg")(chart)
    css = "\n".join((root / "app/static/css" / name).read_text() for name in ("app.css", "chart-tooltips.css"))
    js = (root / "app/static/js/chart-tooltips.js").read_text()
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": width, "height": height}, has_touch=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(f'<html><head><meta name="viewport" content="width=device-width"><style>{css}</style></head><body style="margin:10px">{markup}</body></html>')
        page.add_script_tag(content=js)
        viewport = page.locator("[data-chart-interactive]")
        tooltip = page.locator("[data-chart-tooltip]")

        def coordinates(index):
            return viewport.evaluate("""(el,index)=>{const svg=el.querySelector('svg'),point=JSON.parse(el.dataset.chartPoints)[index];const p=svg.createSVGPoint();p.x=point.x;p.y=(Number(svg.dataset.plotTop)+Number(svg.dataset.plotBottom))/2;const screen=p.matrixTransform(svg.getScreenCTM());return {x:screen.x,y:screen.y}}""", index)

        position = coordinates(1)
        page.mouse.move(position["x"], position["y"])
        playwright.expect(tooltip).to_be_visible()
        playwright.expect(tooltip).to_contain_text("R$ 9.876,54")
        assert tooltip.locator("li").count() == 2  # Séries coincidentes continuam acessíveis.
        assert tooltip.locator("li strong").all_text_contents() == ["R$ 9.876,54", "R$ 9.876,54"]
        assert tooltip.locator("principal").count() == 0  # Label é texto, nunca HTML.
        box = tooltip.bounding_box()
        assert box and box["x"] >= 0 and box["y"] >= 0
        assert box["x"] + box["width"] <= width and box["y"] + box["height"] <= height

        viewport.focus()
        page.keyboard.press("Home")
        playwright.expect(tooltip).to_contain_text("R$ 12.345,67")
        playwright.expect(tooltip).to_contain_text("-R$ 890,12")
        page.keyboard.press("ArrowRight")
        playwright.expect(tooltip).to_contain_text("R$ 9.876,54")
        page.keyboard.press("End")
        playwright.expect(tooltip).to_contain_text("R$ 1.000.000,01")
        assert tooltip.locator("li").count() == 1  # Ausente não vira zero.
        page.keyboard.press("Escape")
        playwright.expect(tooltip).to_be_hidden()

        position = coordinates(0)
        page.touchscreen.tap(position["x"], position["y"])
        playwright.expect(tooltip).to_be_visible()
        playwright.expect(tooltip).to_contain_text("-R$ 890,12")
        tooltip.locator("[data-chart-tooltip-close]").click()
        playwright.expect(tooltip).to_be_hidden()
        assert not errors, errors
        context.close()
        browser.close()
