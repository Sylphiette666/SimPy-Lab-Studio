"""Produce installer wizard art from the application's existing icon."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "build" / "installer-deps"


def main():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    icon = Image.open(ROOT / "src/simlab/static/studio/app.png").convert("RGBA")
    wizard = Image.new("RGB", (328, 628), "#123c3b")
    draw = ImageDraw.Draw(wizard)
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    font = (
        ImageFont.truetype(str(font_path), 34) if font_path.exists() else ImageFont.load_default()
    )
    small = ImageFont.truetype(str(font_path), 16) if font_path.exists() else font
    resized = icon.resize((196, 196), Image.Resampling.LANCZOS)
    wizard.paste(resized, (66, 100), resized)
    draw.text((43, 335), "SimPy Lab", font=font, fill="#ecf8ed")
    draw.text((43, 380), "Studio", font=font, fill="#c7ecb1")
    draw.line((43, 454, 280, 454), fill="#51837b", width=2)
    draw.text((43, 480), "MODEL  /  SIMULATE", font=small, fill="#b9d1c9")
    draw.text((43, 508), "EXPLORE  /  IMPROVE", font=small, fill="#b9d1c9")
    wizard.save(DESTINATION / "wizard-large.bmp")
    corner = Image.new("RGB", (110, 110), "white")
    resized = icon.resize((96, 96), Image.Resampling.LANCZOS)
    corner.paste(resized, (7, 7), resized)
    corner.save(DESTINATION / "wizard-small.bmp")


if __name__ == "__main__":
    main()
