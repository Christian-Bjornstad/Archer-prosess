"""Manual smoke check against a local HTML fixture; no database requests."""
import tempfile
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from archer_processor.services.edge_cdp import EdgeCdpContext
from archer_processor.services.browser_review import BrowserReviewService
from archer_processor.services.capture_validation import CaptureValidation
import json
from dataclasses import replace
from archer_processor.io.tsv_reader import ArcherTsvReader


def main():
    output = Path(tempfile.mkdtemp(prefix="vpm-capture-check-"))
    context = EdgeCdpContext.launch(
        output / "profile", viewport={"width": 1100, "height": 800},
        accept_downloads=False, background=True,
    )
    try:
        page = context.new_page()
        assert page.evaluate("() => ({rows: [1, 2]})") == {"rows": [1, 2]}
        assert page.evaluate("21 * 2") == 42
        assert page.evaluate("async () => 42") == 42
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
        html = """<html><body style="margin:0"><h1>Patient report</h1>
        <div class="accordion-item"><h2 style="background:cyan">Putative unknown variants</h2>
        <div id="scroll" style="height:180px;overflow:auto">
        <table style="width:800px"><tr><th>Gene</th><th>Gene Info</th><th>Alteration</th><th>Evidence</th><th>Reported biomarker(s)</th></tr>
        <tr style="height:300px"><td>TP53</td><td></td><td><span>p.Arg175His</span><span>exon 5</span></td><td>A</td><td></td></tr>
        <tr style="height:300px;background:lime"><td>DDX41</td><td></td><td>p.Arg525His</td><td>B</td><td></td></tr>
        </table></div></div></body></html>"""
        page.goto("data:text/html," + quote(html))
        page.evaluate("document.querySelector('#scroll').scrollTop=80")
        service = BrowserReviewService(profile_root=output, capture_validator=lambda _: CaptureValidation(True, 'ok', 800, 700, 1))
        full = service._capture_mtbp_full_report(page, output, "nested-scroll")
        geometry = json.loads(full.with_suffix('.geometry.json').read_text())
        assert len(geometry['rows']) == 2
        assert geometry['rows'][1]['section']['height'] > 0
        assert geometry['rows'][1]['row']['y'] + geometry['rows'][1]['row']['height'] <= geometry['height']
        assert 'p.Arg175His' in geometry['rows'][0]['identity']
        assert page.evaluate("document.querySelector('#scroll').scrollTop") == 80
        with Image.open(full) as image:
            box = geometry['rows'][1]['row']
            point = (int((box['x'] + box['width'] / 2) * image.width / geometry['width']),
                     int((box['y'] + box['height'] / 2) * image.height / geometry['height']))
            r, g, b = image.convert('RGB').getpixel(point)
            assert g > r + 100 and g > b + 100, (point, (r,g,b))
        print('PASS nested scroll: both rows captured and original scroll restored')
        variant = ArcherTsvReader().read(Path(__file__).resolve().parents[1] / 'tests/fixtures/sample_variants.tsv')[3]
        variant = replace(variant, symbol='DDX41', hgvsc='', hgvsp='p.Arg525His')
        crop = service._crop_mtbp_variant_from_report(page, variant, output, full)
        with Image.open(crop) as image:
            # Section title can be wider than the table; sample inside the row.
            r,g,b = image.convert('RGB').getpixel((image.width//2, image.height-20))
            assert g > r + 100 and g > b + 100
            assert any(g > r + 100 and b > r + 100 for r,g,b
                       in image.convert('RGB').crop((0,0,image.width,35)).getdata())
        print('PASS MTBP: correct second variant cropped from shared report')
        html = """<html><body style="margin:0"><div id="scroll" style="width:500px;height:240px;overflow:auto">
        <div style="width:1000px"><h1 style="background:lime">TP53</h1>
        <gnx-result-page style="display:block;margin-left:100px"><h2>Classification</h2>
        <gnx-result-category style="display:block;margin-top:140px;height:500px">Evidence</gnx-result-category>
        </gnx-result-page></div></div></body></html>"""
        page.goto('data:text/html,' + quote(html))
        page.evaluate("document.querySelector('#scroll').scrollLeft=200")
        overview = output / 'franklin-header.png'
        service._capture_franklin_classification_overview(page, page.locator('gnx-result-page'),
            page.locator('gnx-result-category'), overview, gene_symbol='TP53')
        assert page.evaluate("document.querySelector('#scroll').scrollLeft") == 200
        with Image.open(overview) as image:
            # Green gene header remains in the upper part of the image.
            assert any(g > r + 100 and g > b + 100
                       for r,g,b in image.convert('RGB').crop((0,0,image.width,min(120,image.height))).getdata())
        print('PASS Franklin: gene header captured after horizontal-scroll reset')
        print(output)
    finally:
        context.close()


if __name__ == "__main__":
    main()
