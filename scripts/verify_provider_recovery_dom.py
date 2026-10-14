"""Offline provider recovery smoke checks in a separate, temporary Edge profile.

Uses the app's pure-Python CDP adapter. No credentials, clinical files or provider
submissions are used; all test pages are data URLs containing synthetic HTML.
"""
from pathlib import Path
import sys
import tempfile
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from archer_processor.services.browser_popups import dismiss_franklin_onboarding
from archer_processor.services.browser_review import BrowserReviewService, parse_mtbp_report
from archer_processor.core.models import VariantRecord
from archer_processor.services.edge_cdp import sync_edge_cdp


def main():
    # Keep this isolated from every configured provider profile.
    profile = Path(tempfile.mkdtemp(prefix="vpm-offline-provider-dom-"))
    with sync_edge_cdp() as runtime:
        context = runtime.chromium.launch_persistent_context(str(profile), background=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("data:text/html," + quote(
            "<div role='dialog'><h2>Choose sample material</h2>"
            "<button onclick='window.chosen=true'>Blood</button>"
            "<button onclick='this.parentElement.remove()'>Skip</button></div>"
        ))
        page.locator("[role='dialog']").wait_for(state="visible", timeout=5000)
        assert dismiss_franklin_onboarding(page) == ["Skip"]
        assert not page.evaluate("Boolean(window.chosen)")
        print("PASS: Franklin material dialog skipped without selecting material")

        page.goto("data:text/html," + quote(
            "<table><thead><tr><th>Gene</th><th>Alteration</th>"
            "<th>Functional evidence</th><th>Reported biomarker</th><th>Gene info</th>"
            "</tr></thead><tbody><tr><td rowspan='2'>TESTGENE</td>"
            "<td>p.Arg100SerfsTer5</td><td>Evidence A</td><td>None</td><td>TS</td></tr>"
            "<tr><td>c.100del</td><td>Evidence B</td><td>None</td><td>TS</td>"
            "</tr></tbody></table>"
        ))
        page.locator("table").wait_for(state="visible", timeout=5000)
        rows = BrowserReviewService._extract_expanded_mtbp_rows(page)
        assert len(rows) == 2, f"Expected two reordered/rowspan report rows, got {len(rows)}"
        assert [r["gene"] for r in rows] == ["TESTGENE", "TESTGENE"]
        assert rows[1]["alteration"] == "c.100del"
        assert rows[1]["capture_geometry"]["shared_gene_context"]
        assert rows[1]["capture_geometry"]["row"]["y"] <= rows[0]["capture_geometry"]["row"]["y"]
        print("PASS: MTBP reordered columns and shared gene cells extracted")

        page.goto("data:text/html," + quote(
            "<table><tr><th>Gene</th><th>Gene Info</th><th>Alteration</th>"
            "<th>Functional relevance evidence</th><th>Reported biomarker(s)</th></tr>"
            "<tr><td>TESTGENE</td><td>TS</td><td>Mutation splice region "
            "<a class='smallPopupAction' data-small-popup-id='detail1'>more</a></td>"
            "<td>Evidence B</td><td>None</td></tr>"
            "<tr><td>TESTGENE</td><td>TS</td><td>Mutation 5 prime UTR "
            "<a class='smallPopupAction' data-small-popup-id='detail2'>more</a></td>"
            "<td>Evidence A</td><td>None</td></tr></table>"
            "<div id='small-popup-id-detail1' style='display:none'>"
            "NM_999999.1:c.100del</div>"
            "<div id='small-popup-id-detail2' style='display:none'>"
            "NM_999999.1:c.200del</div>"
        ))
        page.locator("table").wait_for(state="visible", timeout=5000)
        rows = BrowserReviewService._extract_expanded_mtbp_rows(page)
        assert "c.100del" in rows[0]["identity_text"], "Hidden more-details identity was omitted"
        assert rows[0]["functional_evidence"] == "Evidence B"
        requested = VariantRecord(Path("synthetic.tsv"), 1, "SYNTHETIC", "TESTGENE", "NM_999999.1:c.100del")
        result = parse_mtbp_report("", rows, [requested], "synthetic", cancer_type="Blood")
        assert result[BrowserReviewService.variant_key(requested)].status == "found"
        assert not page.locator("#small-popup-id-detail1").is_visible()
        assert "c.100del" in rows[0]["capture_geometry"]["identity"]
        assert "c.200del" not in rows[0]["identity_text"]
        selected, _ = BrowserReviewService(profile_root=profile)._locate_mtbp_screenshot_target(page, requested)
        assert "splice region" in selected.inner_text()
        assert "5 prime UTR" not in selected.inner_text()
        print("PASS: MTBP hidden row details matched without opening or altering the popup")
        context.close()


if __name__ == "__main__":
    main()
