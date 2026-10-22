from pathlib import Path

import pytest

from archer_processor.core.models import VariantRecord
from archer_processor.services.browser_review import BrowserReviewService


class Control:
    def __init__(self, page, label):
        self.page, self.label = page, label

    def count(self):
        return int(self.is_visible())

    def is_visible(self):
        if not self.page.wizard:
            return False
        return self.label == ('Skip question' if self.page.step < 3 else 'Unknown') or (
            self.page.step == 4 and self.label == 'Search')

    def click(self):
        self.page.actions.append(self.label)
        if self.page.stuck:
            return
        if self.label == 'Search':
            self.page.url = 'https://franklin.genoox.com/clinical-db/variant/snpTumor/chr12-1-A-G'
            self.page.wizard = False
        else:
            self.page.step += 1

    def inner_text(self):
        return 'Add your case details What is the cancer type? What is the patient sex?'

    def wait_for(self, **kwargs):
        assert self.is_visible()


class Page:
    url = 'https://franklin.genoox.com/clinical-db/home'

    def __init__(self, wizard=True, stuck=False):
        self.wizard, self.stuck = wizard, stuck
        self.step = 0
        self.actions = []

    def locator(self, selector):
        if selector == 'body':
            control = Control(self, '')
            if not self.wizard:
                control.inner_text = lambda: 'Search Knowledge base'
            return control
        if selector == '.option':
            return type('Options', (), {'count': lambda self: 0})()
        raise AssertionError(selector)

    def get_by_text(self, label, **kwargs):
        return Control(self, label)

    def get_by_role(self, role, *, name, exact=True):
        return Control(self, name)

    def wait_for_timeout(self, ms):
        pass

    def wait_for_url(self, pattern, **kwargs):
        if not pattern.search(self.url):
            raise TimeoutError('the questionnaire was never completed')


def test_first_visit_questions_are_skipped_before_resolving_variant(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    page = Page()
    v = VariantRecord(Path('synthetic.tsv'), 1, 'synthetic', 'KRAS', 'c.437C>T')
    service._open_franklin_resolved_variant(page, v)
    assert page.actions == ['Skip question'] * 3 + ['Unknown', 'Search']


def test_existing_visit_does_not_click_unrelated_controls(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    page = Page(wizard=False)
    assert service._advance_franklin_case_questions(page) is False
    assert not page.actions


def test_stuck_questionnaire_stops_after_four_actions(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    page = Page(stuck=True)
    v = VariantRecord(Path('synthetic.tsv'), 1, 'synthetic', 'KRAS', 'c.437C>T')
    with pytest.raises(TimeoutError, match='questionnaire'):
        service._open_franklin_resolved_variant(page, v)
    assert len(page.actions) == 4
