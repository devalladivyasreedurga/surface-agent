"""The only Surface implementation in this assignment.

Runs headful and persistent for the lifetime of a discovery or replay run so
that a human-escalation handoff (see escalation/) can take over the *same*
visible browser window rather than a fresh session -- there is exactly one
Page object for the run's whole lifetime.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from computer_use.models.actions import (
    Action,
    ActionOutcome,
    ActionResult,
    ActionType,
    Locator,
    LocatorStrategy,
)
from computer_use.models.surface_state import ElementInfo, SurfaceState
from computer_use.surface.accessibility import parse_aria_snapshot
from computer_use.surface.base import Surface
from computer_use.surface.locator_resolver import UnresolvedTargetError, resolve_target

_MAX_TEXT_EXCERPT = 2000
_MAX_ELEMENTS = 200

# Known, deterministically-recognizable "recoverable" interstitial: any
# element exposing the standard ARIA "alertdialog" role (how a real legacy app
# would mark a modal for accessibility -- not a developer-added test hook).
# Native browser dialogs (confirm/alert/prompt) are handled separately above
# via page.on("dialog").


class PlaywrightSurface(Surface):
    def __init__(self, headless: bool = False, evidence_dir: Path | None = None):
        self._pw: Playwright = sync_playwright().start()
        self._browser: Browser = self._pw.chromium.launch(headless=headless)
        self._page: Page = self._browser.new_page()
        self._evidence_dir = evidence_dir
        self._page.set_default_timeout(15_000)
        self._page.on("dialog", lambda d: d.accept())  # unexpected native dialogs: accept and continue

    @property
    def page(self) -> Page:
        """Exposed intentionally so the escalation handoff can attach the
        same live Page to a human operator. No other module should reach
        through this in normal operation."""
        return self._page

    # ---- observe ---------------------------------------------------------

    def observe(self) -> SurfaceState:
        page = self._page
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass

        elements: list[ElementInfo] = []
        try:
            raw = page.locator("body").aria_snapshot()
            elements = parse_aria_snapshot(raw)[:_MAX_ELEMENTS]
        except Exception:
            elements = []

        text_excerpt = None
        try:
            text_excerpt = page.inner_text("body")[:_MAX_TEXT_EXCERPT]
        except Exception:
            pass

        screenshot_ref = None
        if self._evidence_dir is not None:
            screenshot_ref = self._save_screenshot("observe")

        return SurfaceState(
            url=page.url,
            title=self._safe_title(),
            elements=elements,
            visible_text_excerpt=text_excerpt,
            screenshot_ref=screenshot_ref,
        )

    def _safe_title(self) -> str:
        try:
            return self._page.title()
        except Exception:
            return ""

    def save_screenshot(self, tag: str) -> str | None:
        return self._save_screenshot(tag)

    def _save_screenshot(self, tag: str) -> str | None:
        if self._evidence_dir is None:
            return None
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time() * 1000)}_{tag}_{uuid.uuid4().hex[:6]}.png"
        path = self._evidence_dir / name
        try:
            self._page.screenshot(path=str(path))
            return str(path)
        except Exception:
            return None

    # ---- act ---------------------------------------------------------

    def act(self, action: Action) -> ActionResult:
        url_before = self._safe_url()
        try:
            if action.type == ActionType.NAVIGATE:
                self._page.goto(action.value, timeout=action.timeout_ms)
                return ActionResult(outcome=ActionOutcome.OK, detail=f"navigated to {action.value}")

            if action.type == ActionType.WAIT:
                seconds = float(action.value) if action.value else 1.0
                self._page.wait_for_timeout(seconds * 1000)
                return ActionResult(outcome=ActionOutcome.OK, detail=f"waited {seconds}s")

            if action.type in (ActionType.CLICK, ActionType.FILL, ActionType.EXTRACT):
                if action.target is None:
                    return ActionResult(outcome=ActionOutcome.FAILED, detail="action requires a target")
                return self._act_on_target(action)

            return ActionResult(outcome=ActionOutcome.FAILED, detail=f"unsupported action type {action.type}")

        except UnresolvedTargetError as e:
            if self._try_dismiss_known_interstitial():
                try:
                    result = self._act_on_target(action)
                    return ActionResult(
                        outcome=ActionOutcome.RECOVERED,
                        detail=result.detail,
                        extracted=result.extracted,
                        recovery_note="known_interstitial_dismissed",
                    )
                except Exception:
                    pass
            return ActionResult(outcome=ActionOutcome.FAILED, detail=f"could not resolve target: {e}")

        except Exception as e:  # transient load / timeout etc.
            if not self._looks_transient(e):
                return ActionResult(outcome=ActionOutcome.FAILED, detail=str(e))
            return self._recover_from_timeout(action, url_before)

    def _safe_url(self) -> str:
        try:
            return self._page.url
        except Exception:
            return ""

    def _live_url(self, fallback: str) -> str:
        """A live round-trip read of the browser's actual location, used only
        during timeout recovery (see _recover_from_timeout). `Page.evaluate`
        raises while a navigation is actively swapping documents ("execution
        context was destroyed") -- that transient exception means "still in
        flight", so we report the fallback (unchanged) rather than error."""
        try:
            return self._page.evaluate("location.href")
        except Exception:
            return fallback

    def _recover_from_timeout(self, action: Action, url_before: str) -> ActionResult:
        """A client-side wait timed out -- the server may still have been
        mid-response (e.g. our simulated slow load) rather than genuinely
        stuck. We do NOT blindly re-issue the original action: for a
        navigating click, repeating it after the first click's effect has
        already landed server-side would risk a double action (e.g. a double
        form submission) -- exactly the kind of thing a real back-office app
        would punish. Instead we wait briefly, then re-observe: if the URL
        already moved on from where we started, the action's effect evidently
        went through and we report a recovered transient load; only if
        nothing changed do we treat it as safe to retry the action itself.
        """
        # Actively poll for the URL to move rather than trusting
        # wait_for_load_state, which only reports on navigations Playwright's
        # driver already knows are in flight -- it can return immediately
        # against a stale document if it isn't tracking one, understating how
        # long a background response genuinely takes. We poll via a live
        # evaluate() round-trip rather than the cached `page.url` property:
        # empirically, `page.url` can keep reporting the pre-navigation
        # address for seconds after the browser has actually moved on unless
        # something exercises the connection in the meantime.
        deadline = time.time() + 6.0
        while time.time() < deadline and self._live_url(url_before) == url_before:
            time.sleep(0.25)

        if self._live_url(url_before) != url_before:
            return ActionResult(
                outcome=ActionOutcome.RECOVERED,
                detail="action's effect landed after a slow response; page already advanced",
                recovery_note="transient_slow_load",
            )

        try:
            if action.type == ActionType.NAVIGATE:
                self._page.goto(action.value, timeout=action.timeout_ms)
                result = ActionResult(outcome=ActionOutcome.OK, detail=f"navigated to {action.value}")
            else:
                result = self._act_on_target(action)
        except Exception as e2:
            return ActionResult(outcome=ActionOutcome.FAILED, detail=f"failed after transient-recovery retry: {e2}")

        if result.outcome == ActionOutcome.OK:
            return ActionResult(
                outcome=ActionOutcome.RECOVERED,
                detail=result.detail,
                extracted=result.extracted,
                recovery_note="transient_slow_load_retried",
            )
        return result

    @staticmethod
    def _looks_transient(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "timeout" in msg or "navigation" in msg

    def _try_dismiss_known_interstitial(self) -> bool:
        try:
            dialog = self._page.get_by_role("alertdialog")
            if dialog.count() > 0:
                dismiss_btn = dialog.first.get_by_role("button")
                dismiss_btn.first.click(timeout=2000)
                return True
        except Exception:
            pass
        return False

    def _act_on_target(self, action: Action) -> ActionResult:
        pw_locator, matched = resolve_target(self._page, action.target)

        if pw_locator is None and matched.strategy == LocatorStrategy.COORDINATE:
            x_str, y_str = matched.value.split(",")
            x, y = float(x_str), float(y_str)
            if action.type == ActionType.CLICK:
                self._page.mouse.click(x, y)
                return ActionResult(outcome=ActionOutcome.OK, detail="clicked via low-confidence coordinate fallback")
            return ActionResult(outcome=ActionOutcome.FAILED, detail="coordinate strategy only supports click")

        if pw_locator is None:
            return ActionResult(outcome=ActionOutcome.FAILED, detail="target did not resolve")

        if action.type == ActionType.CLICK:
            pw_locator.click(timeout=action.timeout_ms)
            return ActionResult(outcome=ActionOutcome.OK, detail=f"clicked ({matched.strategy.value})")

        if action.type == ActionType.FILL:
            pw_locator.fill(action.value or "", timeout=action.timeout_ms)
            return ActionResult(outcome=ActionOutcome.OK, detail=f"filled ({matched.strategy.value})")

        if action.type == ActionType.EXTRACT:
            text = pw_locator.inner_text(timeout=action.timeout_ms)
            key = action.value or "value"
            return ActionResult(outcome=ActionOutcome.OK, extracted={key: text.strip()})

        return ActionResult(outcome=ActionOutcome.FAILED, detail="unreachable")

    def _element_present(self, element: Locator) -> bool:
        from computer_use.models.actions import Target as _Target

        try:
            pw_locator, _ = resolve_target(self._page, _Target(primary=element))
            return pw_locator is not None and pw_locator.count() > 0
        except UnresolvedTargetError:
            return False

    def _element_visible(self, element: Locator) -> bool:
        """Stricter than _element_present: the element must resolve AND
        actually be visible (not display:none/hidden/zero-size). Distinct
        condition on purpose -- a not-found page and a found-but-loading
        page can both have the target element present in the DOM while only
        one has it genuinely visible."""
        from computer_use.models.actions import Target as _Target

        try:
            pw_locator, _ = resolve_target(self._page, _Target(primary=element))
            return pw_locator is not None and pw_locator.count() > 0 and pw_locator.first.is_visible()
        except UnresolvedTargetError:
            return False
        except Exception:
            return False

    # ---- checkpoint helper (used by replay engine) ------------------------

    def check_condition(
        self,
        *,
        url_contains: str | None,
        element: Locator | None,
        element_visible: Locator | None = None,
        text_contains: str | None,
    ) -> tuple[bool, str, bool]:
        """Returns (ok, detail, recovered_via_interstitial). The third value
        lets the replay engine log a proper RecoveryEvent instead of the
        recovery happening silently -- same visibility action-level recovery
        already gets. Only handles Surface-observable conditions -- engine-
        level conditions (expect_output_present, any_of) are evaluated by
        ReplayEngine itself, which has no business asking a Surface about its
        own internal bookkeeping."""
        if url_contains and url_contains not in self._page.url:
            return False, f"expected url to contain '{url_contains}', got '{self._page.url}'", False

        recovered = False

        if element is not None:
            ok = self._element_present(element)
            if not ok and self._try_dismiss_known_interstitial():
                # Same reasoning as act()'s recovery path: a known interstitial
                # can legitimately be sitting in front of the element a
                # checkpoint is looking for. Dismiss it and check once more
                # before concluding the checkpoint genuinely failed.
                ok = self._element_present(element)
                recovered = recovered or ok
            if not ok:
                return False, f"expected element not found ({element.strategy.value}:{element.value})", False

        if element_visible is not None:
            ok = self._element_visible(element_visible)
            if not ok and self._try_dismiss_known_interstitial():
                ok = self._element_visible(element_visible)
                recovered = recovered or ok
            if not ok:
                return False, f"expected element not visible ({element_visible.strategy.value}:{element_visible.value})", False

        if text_contains:
            try:
                body_text = self._page.inner_text("body")
            except Exception:
                body_text = ""
            if text_contains not in body_text:
                return False, f"expected page text to contain '{text_contains}'", False

        return True, "ok", recovered

    def close(self) -> None:
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass
