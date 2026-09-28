"""Regression test for a real bug found during manual testing: naive
non-exact role+name matching silently resolved to an ancestor container
whose computed accessible name happened to contain the target name as a
substring, instead of the intended element. See REPORT.md, Section 3.
"""
import pytest
from playwright.sync_api import sync_playwright

from computer_use.models.actions import Locator, LocatorStrategy, Target
from computer_use.surface.locator_resolver import UnresolvedTargetError, resolve_target

_AMBIGUOUS_HTML = """
<html><body>
<td class="content">
  <table>
    <tr><td>Savings Balance</td><td aria-label="Savings Balance Value">$4230.55</td></tr>
    <tr><td>Checking Balance</td><td aria-label="Balance">$1875.20</td></tr>
    <tr><td>Reward Balance</td><td aria-label="Balance">$12.00</td></tr>
  </table>
</td>
</body></html>
"""


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        pg.set_content(_AMBIGUOUS_HTML)
        yield pg
        browser.close()


def test_exact_match_resolves_to_the_intended_element_not_the_ancestor(page):
    target = Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Savings Balance Value"))
    pw_locator, matched = resolve_target(page, target)
    assert pw_locator is not None
    assert pw_locator.inner_text() == "$4230.55"


def test_two_elements_sharing_an_exact_name_are_never_guessed_between(page):
    # Two cells are deliberately given the identical aria-label "Balance" --
    # a genuinely ambiguous query. The old buggy resolver would have grabbed
    # `.first` here; the fixed one must refuse to guess.
    target = Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Balance"))
    with pytest.raises(UnresolvedTargetError):
        resolve_target(page, target)


def test_coordinate_locator_is_always_low_confidence():
    loc = Locator(strategy=LocatorStrategy.COORDINATE, value="100,200", confidence="high")
    assert loc.confidence == "low"
