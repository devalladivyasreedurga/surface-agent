"""Regression test for a real bug found while generating evidence: checkpoint
verification (`check_condition`) originally had no interstitial-recovery path
of its own, even though `act()` did -- so adding a checkpoint on a step made
replay fail on a page with a known, dismissable interstitial instead of
recovering. See REPORT.md, Determinism & error handling, and
evidence/README.md.
"""
import pytest

from computer_use.models.actions import Locator, LocatorStrategy
from computer_use.surface.playwright_surface import PlaywrightSurface

_HTML = """
<html><body>
<div role="alertdialog" aria-label="Notice">
  <button onclick="
    document.querySelector('[role=alertdialog]').remove();
    document.body.insertAdjacentHTML('beforeend', '&lt;div role=&quot;cell&quot; aria-label=&quot;Balance Value&quot;&gt;42&lt;/div&gt;');
  ">Dismiss</button>
</div>
</body></html>
"""


@pytest.fixture
def surface():
    s = PlaywrightSurface(headless=True)
    s.page.set_content(_HTML)
    yield s
    s.close()


def test_check_condition_recovers_from_a_known_interstitial(surface):
    element = Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Balance Value")

    ok, detail, recovered = surface.check_condition(url_contains=None, element=element, text_contains=None)

    assert ok is True
    assert recovered is True


def test_check_condition_fails_cleanly_when_nothing_can_be_recovered(surface):
    # No interstitial present, and the element genuinely never appears.
    surface.page.set_content("<html><body><p>Nothing here.</p></body></html>")
    element = Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Balance Value")

    ok, detail, recovered = surface.check_condition(url_contains=None, element=element, text_contains=None)

    assert ok is False
    assert recovered is False
    assert "not found" in detail
