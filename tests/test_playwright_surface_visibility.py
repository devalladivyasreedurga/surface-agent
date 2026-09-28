"""Proves expect_element_visible is genuinely stricter than expect_element.

Note on what actually demonstrates this: display:none / visibility:hidden
elements are excluded from Chromium's accessibility tree entirely, so an
accessibility-role locator already returns zero matches for those -- there
is no presence/visibility gap to observe there, `expect_element` already
fails too. The real gap shows up for an element that IS exposed to the
accessibility tree (e.g. a loading placeholder collapsed to zero size before
its content arrives) but has no rendered box -- exactly the "found but still
loading" vs. "not found" distinction a checkpoint needs to tell apart.
"""
import pytest

from computer_use.models.actions import Locator, LocatorStrategy
from computer_use.surface.playwright_surface import PlaywrightSurface

_HTML = """
<html><body>
<div role="cell" aria-label="Loading Balance" style="width:0;height:0;overflow:hidden;">$999.99</div>
<div role="cell" aria-label="Visible Balance">$4230.55</div>
</body></html>
"""


@pytest.fixture
def surface():
    s = PlaywrightSurface(headless=True)
    s.page.set_content(_HTML)
    yield s
    s.close()


def test_zero_size_element_passes_presence_but_fails_visibility(surface):
    loading = Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Loading Balance")

    present_ok, _, _ = surface.check_condition(url_contains=None, element=loading, element_visible=None, text_contains=None)
    visible_ok, _, _ = surface.check_condition(url_contains=None, element=None, element_visible=loading, text_contains=None)

    assert present_ok is True   # resolves in the accessibility tree -- it exists
    assert visible_ok is False  # but has no rendered box -- not actually visible yet


def test_visible_element_passes_both(surface):
    visible = Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Visible Balance")

    present_ok, _, _ = surface.check_condition(url_contains=None, element=visible, element_visible=None, text_contains=None)
    visible_ok, _, _ = surface.check_condition(url_contains=None, element=None, element_visible=visible, text_contains=None)

    assert present_ok is True
    assert visible_ok is True
