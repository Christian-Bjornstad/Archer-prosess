import base64
import io
import json

import pytest
from PIL import Image

from archer_processor.core import VariantProcessor
from archer_processor.services.browser_review import BrowserReviewService
from archer_processor.services.capture_validation import CaptureValidation, IncompleteCaptureError
from archer_processor.services.edge_cdp import EdgeCdpPage
from pathlib import Path


def test_clip_maps_css_to_scaled_document_pixels(tmp_path):
    source = Image.new("RGB", (800, 600), "white")
    source.paste("red", (240, 160, 400, 240))
    encoded = io.BytesIO()
    source.save(encoded, format="PNG")

    class Connection:
        def call(self, method, params=None):
            if method == "Page.getLayoutMetrics":
                return {"cssContentSize": {"width": 400, "height": 300}}
            assert method == "Page.captureScreenshot"
            assert params["clip"]["x"] == 0
            return {"data": base64.b64encode(encoded.getvalue()).decode()}

    page = object.__new__(EdgeCdpPage)
    page._connection = Connection()
    page._evaluate_value = lambda _: {"x": 20, "y": 30}
    output = tmp_path / "clip.png"
    page.screenshot(path=str(output), clip={"x": 100, "y": 50, "width": 80, "height": 40})
    with Image.open(output) as cropped:
        assert cropped.size == (160, 80)
        assert cropped.getextrema() == ((255, 255), (0, 0), (0, 0))


@pytest.mark.parametrize("ambiguous_identity", [False, True])
def test_mtbp_crops_only_exact_row_from_saved_patient_capture(tmp_path, ambiguous_identity):
    result = VariantProcessor().process(Path(__file__).parent / "fixtures/sample_variants.tsv", "2026-09-05", tmp_path / "review.xlsx")
    variant = result.variants[3]
    service = BrowserReviewService(profile_root=tmp_path, capture_validator=lambda _: CaptureValidation(True, "ok", 100, 100, 1.0))
    full = tmp_path / "patient.png"
    source = Image.new("RGB", (400, 400), "white")
    source.paste("blue", (0, 0, 400, 40))
    source.paste("red", (0, 160, 400, 240))
    source.save(full)
    entry = {"gene": variant.symbol, "identity": variant.hgvsc,
             "header": {"x": 0, "y": 0, "width": 200, "height": 20},
             "row": {"x": 0, "y": 80, "width": 200, "height": 40}}
    geometry = {"width": 200, "height": 200, "rows": [entry]}
    metadata = full.with_suffix(".geometry.json")
    metadata.write_text(json.dumps(geometry))
    output = service._crop_mtbp_variant_from_report(None, variant, tmp_path, full)
    with Image.open(output) as cropped:
        assert cropped.size == (400, 120)
        assert cropped.getpixel((20, 20)) == (0, 0, 255)
        assert cropped.getpixel((20, 70)) == (255, 0, 0)
    geometry["rows"].append({**entry, "identity": "c.9999G>A",
                             "row": {"x": 0, "y": 140, "width": 200, "height": 40}})
    metadata.write_text(json.dumps(geometry))
    output = service._crop_mtbp_variant_from_report(None, variant, tmp_path, full)
    with Image.open(output) as cropped:
        assert cropped.size == (400, 120)
        assert cropped.info["match_scope"] == "exact_variant"
    geometry["rows"] = [entry]
    # When no exact identity is available, preserve ALL rows of this gene,
    # including section context, but never another gene's pixels.
    source.paste("yellow", (0, 80, 400, 120))
    source.paste("green", (0, 280, 400, 360))
    source.save(full)
    entry["identity"] = variant.hgvsc if ambiguous_identity else "Mutation"
    entry["section"] = {"x": 0, "y": 40, "width": 200, "height": 20}
    geometry["rows"].extend([
        {**entry, "row": {"x": 0, "y": 140, "width": 200, "height": 40}},
        {**entry, "gene": "OTHER"},
    ])
    metadata.write_text(json.dumps(geometry))
    output = service._crop_mtbp_variant_from_report(None, variant, tmp_path, full)
    with Image.open(output) as cropped:
        colours = set(cropped.convert("RGB").getdata())
        assert {(255, 255, 0), (255, 0, 0), (0, 128, 0)} <= colours
        assert cropped.info["match_scope"] == "gene_context"
        assert "ikke entydig" in cropped.info["caption"]
    geometry["rows"] = [{**entry, "gene": "OTHER"}]
    metadata.write_text(json.dumps(geometry))
    with pytest.raises(IncompleteCaptureError):
        service._crop_mtbp_variant_from_report(None, variant, tmp_path, full)
