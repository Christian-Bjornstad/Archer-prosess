"""Synthetic provider pages; no patient data or live credentials."""
from pathlib import Path

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.browser_review import BrowserReviewService
from archer_processor.services.browser_review import _cosmic_migration_target
from archer_processor.services.provider_failures import ProviderFailureKind, ProviderLookupError


def variant():
    return VariantRecord(Path('synthetic.tsv'), 1, 'synthetic', 'TP53',
                         'NM_000546.6:c.405C>G', hgvsp='p.Cys135Trp', cosmic_id='COSM437604')


LIVE_NOTICE = '''The legacy mutation COSM437604 has now been merged into the following mutation.
Genomic Mutation ID
COSV52669330 Legacy Identifier
COSM44219 Gene name TP53 AA mutation p.C135W CDS mutation c.405C>G'''


def test_live_cosmic_v104_migration_notice_uses_legacy_identifier():
    assert _cosmic_migration_target(LIVE_NOTICE, 'COSM437604') == 'COSM44219'


def test_live_migration_notice_cannot_borrow_an_unrelated_legacy_id():
    with pytest.raises(ProviderLookupError):
        _cosmic_migration_target(LIVE_NOTICE.replace('COSM437604', 'COSM999'), 'COSM437604')


class Page:
    def __init__(self, notice):
        self.url = ''
        self.notice = notice
        self.visited = []

    def goto(self, url, **kwargs):
        self.url = url
        self.visited.append(url)

    def locator(self, selector):
        assert selector == 'body'
        return self

    def inner_text(self, **kwargs):
        return self.notice

    def wait_for_timeout(self, milliseconds):
        pass


def setup_service(tmp_path, monkeypatch, notice):
    service = BrowserReviewService(profile_root=tmp_path)
    page = Page(notice)
    assemblies = []
    monkeypatch.setattr('archer_processor.services.browser_review.dismiss_known_overlays', lambda p: None)
    monkeypatch.setattr(service, '_ensure_cosmic_genome', lambda p, genome: assemblies.append(genome))
    monkeypatch.setattr(service, '_ensure_cosmic_grch37', lambda p: assemblies.append('37'))
    monkeypatch.setattr(service, '_open_cosmic_mutation_page', lambda p, v: None)

    def resolve(p, v):
        if v.cosmic_id == 'COSM437604':
            raise ProviderLookupError(ProviderFailureKind.NOT_FOUND, 'mutation not found')
        p.url = 'https://cancer.sanger.ac.uk/cosmic/mutation/overview?genome=37&id=999&merge=44219'

    monkeypatch.setattr(service, '_resolve_cosmic_mutation_page', resolve)
    monkeypatch.setattr(service, '_wait_for_cosmic_result', lambda p: None)
    monkeypatch.setattr(service, '_verify_cosmic_grch37', lambda p: None)
    captured = []

    def capture(v, p, directory, **kwargs):
        captured.append((v.cosmic_id, kwargs))
        return DatabaseEvidence('COSMIC', 'found', 'captured', accession=v.cosmic_id, url=p.url)

    monkeypatch.setattr(service, '_capture_cosmic_result', capture)
    return service, page, assemblies, captured


def test_grch38_migration_returns_grch37_result_with_trace(tmp_path, monkeypatch):
    service, page, assemblies, captured = setup_service(
        tmp_path, monkeypatch, LIVE_NOTICE)
    original = variant()
    result = service._lookup_cosmic_with_retry(page, original, service.query_url('COSMIC', original), tmp_path, progress=None)
    assert result.status == 'found'
    assert result.accession == 'COSM44219'
    assert result.raw['original_cosmic_id'] == 'COSM437604'
    assert result.raw['resolved_cosmic_id'] == 'COSM44219'
    assert result.raw['cosmic_id_changed'] is True
    assert result.raw['assembly_verified'] == 'GRCh37'
    assert 'endret cosmic id' in result.summary
    assert 'genome=38' in page.visited[1]
    assert 'COSM44219' in page.visited[-1]
    assert assemblies[0] == '38' and assemblies[-1] == '37'
    assert captured == [('COSM44219', {'require_variant_identity': True})]
    assert original.cosmic_id == 'COSM437604'


@pytest.mark.parametrize('notice', [
    'Related mutations COSM44219 and COSM437604',
    'COSM437604 has been merged into COSM44219. COSM437604 has been merged into COSM99.',
    'COSM44219 has been merged into COSM437604.',
])
def test_unproven_or_ambiguous_migration_is_not_found(tmp_path, monkeypatch, notice):
    service, page, assemblies, captured = setup_service(tmp_path, monkeypatch, notice)
    v = variant()
    result = service._lookup_cosmic_with_retry(page, v, service.query_url('COSMIC', v), tmp_path, progress=None)
    assert result.status in {'not_found', 'ambiguous'}
    assert not captured
    assert assemblies[-1] == '37'


def test_excel_surfaces_both_ids_in_overview_and_variant_sheet(tmp_path):
    import openpyxl
    from archer_processor.core.models import ProcessingResult
    from archer_processor.reports.patient_excel import PatientExcelReportWriter
    v = variant()
    evidence = DatabaseEvidence('COSMIC', 'found', 'captured', accession='COSM44219',
        url='https://cancer.sanger.ac.uk/cosmic/mutation/overview?genome=37&id=999',
        raw={'cosmic_id_changed': True, 'original_cosmic_id': 'COSM437604', 'resolved_cosmic_id': 'COSM44219'})
    output = tmp_path / 'report.xlsx'
    writer = PatientExcelReportWriter()
    result = ProcessingResult(Path('synthetic.tsv'), None, '2026-10-22', [v], [])
    writer.write_patient(result, 'synthetic', [v], output, {writer._key(v): [evidence]})
    book = openpyxl.load_workbook(output)
    try:
        label = book['Oversikt']['J8'].value
        assert 'endret cosmic id' in label
        assert 'COSM437604' in label and 'COSM44219' in label
        assert book['Oversikt']['J8'].hyperlink.target == evidence.url
        assert 'endret cosmic id' in book['Oversikt']['D8'].value
        assert any('endret cosmic id' in str(c.value) for row in book['TP53'] for c in row)
    finally:
        book.close()


def test_migrated_id_alone_cannot_satisfy_variant_identity(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    v = variant()
    v.cosmic_id = 'COSM44219'
    page = Page('TP53 COSM44219 c.999A>T p.K333N')
    result = service._capture_cosmic_result(v, page, tmp_path, require_variant_identity=True)
    assert result.status == 'identity_mismatch'
    assert not list(tmp_path.glob('*.png'))


def test_direct_migration_banner_cannot_bypass_variant_identity(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    page = Page('COSM437604 has been merged into COSM44219. TP53 c.999A>T')
    result = service._capture_cosmic_result(variant(), page, tmp_path)
    assert result.status == 'identity_mismatch'


def test_migration_is_not_attempted_on_ambiguous_primary_result(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    page = Page('')
    monkeypatch.setattr('archer_processor.services.browser_review.dismiss_known_overlays', lambda p: None)
    def ambiguous(p, v):
        raise ProviderLookupError(ProviderFailureKind.AMBIGUOUS, 'multiple matches')
    monkeypatch.setattr(service, '_resolve_cosmic_mutation_page', ambiguous)
    monkeypatch.setattr(service, '_lookup_cosmic_migration', lambda *a, **kw: pytest.fail('must stop'))
    v = variant()
    result = service._lookup_cosmic_with_retry(page, v, service.query_url('COSMIC', v), tmp_path, progress=None)
    assert result.status == 'ambiguous'
