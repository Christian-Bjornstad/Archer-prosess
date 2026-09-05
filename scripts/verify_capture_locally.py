"""Manual smoke check against a local HTML fixture; no database requests."""
import tempfile
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from archer_processor.services.edge_cdp import EdgeCdpContext


def main():
    output = Path(tempfile.mkdtemp(prefix="vpm-capture-check-"))
    context = EdgeCdpContext.launch(
        output / "profile", viewport={"width": 1100, "height": 800},
        accept_downloads=False, background=True,
    )
    try:
        page = context.new_page()
        html = """<html><body style="margin:0;height:1400px">
        <div id="target" style="position:absolute;left:173px;top:130px;
        width:240px;height:90px;background:rgb(255,0,0)"></div></body></html>"""
        page.goto("data:text/html," + quote(html))
        for zoom in (1, 1.25, 0.8):
            page.evaluate(f"document.body.style.zoom = '{zoom}'")
            target = output / f"clip-{zoom}.png"
            page.locator("#target").screenshot(path=str(target))
            with Image.open(target) as image:
                # Rounding can add a single edge pixel; the inset must be red.
                interior = image.convert("RGB").crop((2, 2, image.width - 2, image.height - 2))
                red, green, blue = interior.getextrema()
                # Edge applies the display colour profile (pure CSS red can
                # become e.g. 242/0/0). Uniformity verifies the crop edges.
                assert red[0] == red[1] and red[0] > 200
                assert green == blue == (0, 0)
                print(f"PASS zoom={zoom}: {image.size}")
        print(output)
    finally:
        context.close()


if __name__ == "__main__":
    main()
