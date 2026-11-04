"""Rebuild the ICO from the small repo-native SVG, with no runtime dependency.

Authoring only: requires Pillow. Run `python scripts/build_favicon.py` from any
directory. Only this asset's rect/polygon/circle primitives are supported; the
SVG remains the source of truth for geometry and colours.
"""
from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend/public/favicon.svg"
OUTPUT = ROOT / "frontend/public/favicon.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> Image.Image:
    root = ET.parse(SOURCE).getroot()
    if root.get("viewBox") != "0 0 32 32":
        raise ValueError("Expected 32px favicon viewBox")
    supersample = 8
    scale = size * supersample / 32
    image = Image.new("RGBA", (size * supersample, size * supersample))
    draw = ImageDraw.Draw(image)
    for element in root:
        kind = element.tag.rsplit("}", 1)[-1]
        if kind in {"title", "desc"}:
            continue
        colour = element.attrib["fill"]
        if kind == "rect":
            x, y = float(element.get("x", "0")), float(element.get("y", "0"))
            width, height = float(element.attrib["width"]), float(element.attrib["height"])
            radius = float(element.get("rx", "0"))
            draw.rounded_rectangle((x * scale, y * scale, (x + width) * scale, (y + height) * scale), radius=radius * scale, fill=colour)
        elif kind == "polygon":
            points = [tuple(float(value) * scale for value in point.split(",")) for point in element.attrib["points"].split()]
            draw.polygon(points, fill=colour)
        elif kind == "circle":
            x, y, radius = (float(element.attrib[key]) for key in ("cx", "cy", "r"))
            draw.ellipse(((x - radius) * scale, (y - radius) * scale, (x + radius) * scale, (y + radius) * scale), fill=colour)
        else:
            raise ValueError(f"Unsupported favicon primitive: {kind}")
    return image.resize((size, size), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    frames = [render(size) for size in SIZES]
    frames[-1].save(OUTPUT, format="ICO", sizes=[(size, size) for size in SIZES], append_images=frames[:-1])
    print(f"Built {OUTPUT.name}: " + ", ".join(f"{size}x{size}" for size in SIZES))
