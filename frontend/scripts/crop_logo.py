"""One-shot: crop fpulse-logo.png to its mark and save a tight square variant.

Reads fpulse-logo.png (white background + padding), finds the bounding box
of non-near-white pixels, centers it in a square with a small breathing margin,
and writes fpulse-logo-mark.png (256x256, optimized PNG).
"""

from PIL import Image, ImageChops
from pathlib import Path

HERE = Path(__file__).parent
PUBLIC = HERE.parent / "public"
SRC = PUBLIC / "fpulse-logo.png"
DST = PUBLIC / "fpulse-logo-mark.png"

# How much each channel may differ from the sampled background before a pixel
# counts as "logo". Anti-aliased mark edges blend with the bg so we want a
# tolerance generous enough to catch the gradient but tight enough to reject
# bg noise.
BG_TOLERANCE = 18
# How much breathing room to add around the detected mark (fraction of mark size).
PADDING_FRAC = 0.06
# Output canvas size (square). 256 is plenty for a 36px chip @ 4x DPI.
OUT_SIZE = 256


def main() -> None:
    im = Image.open(SRC).convert("RGB")
    w, h = im.size

    # Sample the four corners (averaged) to detect actual background color.
    corners = [im.getpixel((0, 0)), im.getpixel((w - 1, 0)),
               im.getpixel((0, h - 1)), im.getpixel((w - 1, h - 1))]
    bg = tuple(int(sum(c[i] for c in corners) / 4) for i in range(3))
    print(f"Detected background: rgb{bg}")

    # Build a difference image vs a uniform-bg image, then a mask of
    # significantly-different pixels.
    bg_im = Image.new("RGB", (w, h), bg)
    diff = ImageChops.difference(im, bg_im)
    # Convert to grayscale "how different" and threshold.
    gray = diff.convert("L").point(lambda v: 255 if v > BG_TOLERANCE else 0)
    bbox = gray.getbbox()
    if bbox is None:
        raise SystemExit("No non-background pixels found — check tolerance.")
    min_x, min_y, max_x_excl, max_y_excl = bbox
    max_x, max_y = max_x_excl - 1, max_y_excl - 1

    mark_w = max_x - min_x + 1
    mark_h = max_y - min_y + 1
    print(f"Mark bounds: x={min_x}..{max_x} y={min_y}..{max_y} ({mark_w}x{mark_h}) of {w}x{h}")

    # Square crop centered on the mark with padding.
    side = max(mark_w, mark_h)
    pad = int(side * PADDING_FRAC)
    side += 2 * pad
    cx = (min_x + max_x) / 2
    cy = (min_y + max_y) / 2
    left = int(round(cx - side / 2))
    top = int(round(cy - side / 2))
    right = left + side
    bottom = top + side

    # Clamp inside the canvas; if the centered square spills out, shift it.
    if left < 0:
        right -= left
        left = 0
    if top < 0:
        bottom -= top
        top = 0
    if right > w:
        left -= right - w
        right = w
    if bottom > h:
        top -= bottom - h
        bottom = h

    print(f"Crop box: ({left}, {top}, {right}, {bottom})")
    cropped = im.crop((left, top, right, bottom))

    # Resize to OUT_SIZE x OUT_SIZE with high-quality resampling.
    resized = cropped.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)

    # Save flattened on white (matches the chip background we set in the UI).
    out = Image.new("RGB", (OUT_SIZE, OUT_SIZE), (255, 255, 255))
    out.paste(resized, (0, 0))
    out.save(DST, "PNG", optimize=True)

    src_kb = SRC.stat().st_size / 1024
    dst_kb = DST.stat().st_size / 1024
    print(f"Wrote {DST.name}: {dst_kb:.1f} KB (source was {src_kb:.1f} KB)")


if __name__ == "__main__":
    main()
