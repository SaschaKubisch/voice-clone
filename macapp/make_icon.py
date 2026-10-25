"""Draws the Voice Clone app icon as a 1024 x 1024 PNG: a sound wave on a rounded square.

Usage: .venv/bin/python macapp/make_icon.py <output.png>
make-app.sh turns the PNG into an .icns file with sips and iconutil.
"""
import sys

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024          # final size in pixels
SCALE = 4            # drawn at 4x and scaled down, for smooth edges
BODY = 824           # the rounded square; macOS icons keep a transparent margin around it
RADIUS = 185
BACKGROUND = (30, 42, 74, 255)    # deep blue
BARS = (244, 241, 234, 255)       # warm white
ACCENT = (255, 122, 89, 255)      # coral, for the tallest bar
HEIGHTS = [0.26, 0.48, 0.76, 1.0, 0.68, 0.44, 0.24]   # share of the tallest bar
ACCENT_INDEX = 3
BAR_WIDTH, BAR_GAP, TALLEST = 56, 34, 470


def main(out_path):
    s = SCALE
    canvas = Image.new("RGBA", (SIZE * s, SIZE * s), (0, 0, 0, 0))
    margin = (SIZE - BODY) // 2
    box = [margin * s, margin * s, (margin + BODY) * s - 1, (margin + BODY) * s - 1]

    # soft shadow under the square
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [box[0], box[1] + 12 * s, box[2], box[3] + 12 * s], RADIUS * s, fill=(0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14 * s))
    canvas = Image.alpha_composite(canvas, shadow)

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(box, RADIUS * s, fill=BACKGROUND)

    total = len(HEIGHTS) * BAR_WIDTH + (len(HEIGHTS) - 1) * BAR_GAP
    x = (SIZE - total) // 2
    center_y = SIZE // 2
    for i, share in enumerate(HEIGHTS):
        h = TALLEST * share
        draw.rounded_rectangle(
            [x * s, (center_y - h / 2) * s, (x + BAR_WIDTH) * s, (center_y + h / 2) * s],
            BAR_WIDTH * s // 2, fill=ACCENT if i == ACCENT_INDEX else BARS)
        x += BAR_WIDTH + BAR_GAP

    canvas.resize((SIZE, SIZE), Image.LANCZOS).save(out_path)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
