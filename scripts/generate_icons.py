from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "static" / "icons"
BG = "#163b40"
PALE = "#c8e2cb"
WHITE = "#f6f5f1"
GOLD = "#e4a761"
Point = tuple[int, int]
Curve = tuple[Point, Point, Point, Point]


def bezier(points: Curve, steps: int = 100):
    p0, p1, p2, p3 = points
    out_points: list[tuple[float, float]] = []
    for step in range(steps + 1):
        t = step / steps
        u = 1 - t
        x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
        out_points.append((x, y))
    return out_points


def create_icon(size: int) -> Image.Image:
    scale = size / 512
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=round(116 * scale), fill=BG)
    stroke = max(2, round(38 * scale))
    curves: list[tuple[Curve, Curve, str]] = [
        (((118, 300), (146, 230), (195, 189), (256, 189)), ((256, 189), (307, 189), (345, 216), (394, 280)), PALE),
        (((118, 212), (151, 284), (196, 323), (256, 323)), ((256, 323), (310, 323), (352, 294), (394, 231)), WHITE),
    ]
    for first, second, color in curves:
        draw.line(bezier(first) + bezier(second)[1:], fill=color, width=stroke, joint="curve")
    radius = round(23 * scale)
    center = (round(256 * scale), round(256 * scale))
    draw.ellipse((center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius), fill=GOLD)
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for filename, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        create_icon(size).save(OUT / filename, format="PNG", optimize=True)
        print(f"Criado {filename} ({size}x{size})")


if __name__ == "__main__":
    main()
