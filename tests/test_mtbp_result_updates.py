from pathlib import Path
import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.browser_review import BrowserReviewService, parse_mtbp_report


def test_mtbp_snapshot_is_published_before_capture_and_is_independent(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    variant = VariantRecord(Path('synthetic.tsv'), 1, 'synthetic', 'TP53', 'c.524G>A')
    key = service.variant_key(variant)
    updates = []

    def search(database, variants, directory, **kwargs):
        result = DatabaseEvidence('MTBP', 'found', 'matched', raw={
            'lookup_status': 'found', 'analysis_id': 'ARCHER-synthetic'})
        service._publish_mtbp_results({key: result})
        assert len(updates) == 1
        result.status = 'partial_capture'
        result.raw['capture_error'] = 'permission denied'
        return {key: result}

    monkeypatch.setattr(service, '_search_database', search)
    result = service.search_variants([variant], ['MTBP'], tmp_path, checkpoint=updates.append)
    assert updates[0][key][0].raw['lookup_status'] == 'found'
    assert updates[0][key][0].status == 'partial_capture'
    assert 'capture_error' not in updates[0][key][0].raw
    assert result[key][0].status == 'partial_capture'
    assert service._result_checkpoint is None


def test_mtbp_parser_retains_lookup_outcome_for_missing_rows():
    variant = VariantRecord(Path('synthetic.tsv'), 1, 'synthetic', 'TP53', 'c.524G>A')
    result = parse_mtbp_report('', [], [variant], '', cancer_type='Blood')
    assert result['synthetic|c.524G>A'].raw['lookup_status'] == 'not_found'


@pytest.mark.parametrize('error_type', [PermissionError, TimeoutError, RuntimeError])
def test_mtbp_capture_failure_keeps_parsed_found_and_missing_results(tmp_path, monkeypatch, error_type):
    service = BrowserReviewService(profile_root=tmp_path)
    variants = [VariantRecord(Path('synthetic.tsv'), 1, 'synthetic', 'TP53', 'c.524G>A'),
                VariantRecord(Path('synthetic.tsv'), 2, 'synthetic', 'KRAS', 'c.35G>A')]
    updates = []
    service._result_checkpoint = updates.append
    capture_readiness = []

    class Locator:
        def inner_text(self, **kwargs):
            return 'Human genome: GRCh37/hg19\nCancer type: Blood'
        def count(self):
            return 0
        def is_visible(self):
            return True
        def is_enabled(self):
            return True
        def click(self):
            page.url = 'https://mtbp.org/patients/1/report/1/'

    class Page:
        url = 'https://mtbp.org/analyse/'
        def locator(self, selector):
            return Locator()

    page = Page()

    class Runtime:
        def __init__(self):
            self.chromium = self
            self.pages = [page]
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def launch_persistent_context(self, *args, **kwargs):
            return self
        def close(self):
            pass

    monkeypatch.setattr(service, '_browser_api', lambda: (Runtime, Exception, TimeoutError))
    monkeypatch.setattr(service, '_goto_with_retries', lambda p, url, **kw: setattr(p, 'url', url))
    monkeypatch.setattr(service, '_session_authenticated', lambda *a: True)
    monkeypatch.setattr(service, '_cleanup_stale_mtbp_reports', lambda *a, **kw: {})
    monkeypatch.setattr(service, '_fill_mtbp_form', lambda *a: None)
    monkeypatch.setattr(service, '_wait_for_mtbp_acceptance', lambda p: '')
    monkeypatch.setattr(service, '_extract_mtbp_rows', lambda p: [{
        'gene': 'TP53', 'identity_text': 'c.524G>A', 'section': 'Functionally relevant',
        'functional_evidence': 'Evidence A (Curated)'}])

    def denied(*args):
        capture_readiness.append(bool(updates) and len(updates[0]) == 2)
        raise error_type('capture unavailable')

    monkeypatch.setattr(service, '_capture_mtbp_full_report', denied)
    monkeypatch.setattr(service, '_capture_mtbp_variant_with_fallback', denied)
    result = service._search_mtbp_batch(variants, tmp_path / 'audit', progress=None)
    assert capture_readiness and all(capture_readiness), 'results must be visible before any screenshot write'
    assert updates and len(updates[0]) == 2
    found, missing = (result[service.variant_key(v)] for v in variants)
    assert found.status == 'partial_capture'
    assert found.raw['lookup_status'] == 'found'
    assert found.clinical_significance == 'Functionally relevant'
    assert missing.status == 'not_found'
    assert missing.raw['lookup_status'] == 'not_found'


def test_unwritable_audit_does_not_lose_mtbp_result_or_delete_remote_report(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    evidence = DatabaseEvidence('MTBP', 'found', 'matched', raw={'lookup_status': 'found'})
    def denied(*args):
        raise PermissionError('read only directory')
    monkeypatch.setattr(service, '_persist_mtbp_report_audit', denied)
    monkeypatch.setattr(service, '_delete_mtbp_report', lambda *a: (_ for _ in ()).throw(AssertionError('must retain report')))
    variant = VariantRecord(Path('synthetic.tsv'), 1, 'synthetic', 'TP53', 'c.524G>A')
    outcome = service._finalize_mtbp_report(object(), 'ARCHER-synthetic', [(tmp_path / 'audit.json', variant, evidence)])
    assert outcome['status'] == 'pending'
    assert evidence.status == 'found'
    assert evidence.raw['lookup_status'] == 'found'
    assert evidence.raw['audit_write_error']
