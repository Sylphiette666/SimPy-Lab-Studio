"""Build a reproducible geometric application icon; no external image assets."""

from pathlib import Path

from PIL import Image, ImageDraw

TARGET = Path(__file__).resolve().parents[1] / "src" / "simlab" / "static" / "studio"


def main():
    factor = 4
    canvas = Image.new("RGBA", (256 * factor, 256 * factor))
    draw = ImageDraw.Draw(canvas)

    def rectangle(box, color, radius=0):
        box = tuple(n * factor for n in box)
        draw.rounded_rectangle(box, radius=radius * factor, fill=color)

    rectangle((4, 4, 252, 252), "#123c3b", 54)
    rectangle((28, 28, 228, 228), "#1d5552", 35)
    # Three machines and their material flow share the studio's green palette.
    rectangle((47, 126, 87, 178), "#c4edda", 10)
    rectangle((108, 95, 148, 178), "#6fdbb0", 10)
    rectangle((169, 62, 209, 178), "#d3f49f", 10)
    draw.line(
        [(45 * factor, 196 * factor), (210 * factor, 196 * factor)],
        fill="#9ccbbe",
        width=8 * factor,
    )
    for x in (64, 105, 146, 187):
        rectangle((x - 5, 208, x + 5, 218), "#9ccbbe", 5)
    draw.line(
        [(52 * factor, 95 * factor), (86 * factor, 62 * factor), (125 * factor, 62 * factor)],
        fill="#eef7eb",
        width=7 * factor,
    )
    draw.polygon(
        [(128 * factor, 62 * factor), (112 * factor, 51 * factor), (112 * factor, 73 * factor)],
        fill="#eef7eb",
    )
    resized = canvas.resize((256, 256), Image.Resampling.LANCZOS)
    resized.save(TARGET / "app.png")
    resized.save(TARGET / "app.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])


if __name__ == "__main__":
    main()
