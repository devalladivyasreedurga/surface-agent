"""A deliberately small, deliberately legacy-feeling back-office banking app.

Server-rendered HTML, table-based layout, no data-testid/CSS hooks for
automation anywhere -- accessible labels/roles are the only reliable surface,
which is the point (see assignment Section 1: "the only reliable surface is
what a human operator sees and does"). This app exists to exercise the
automation system; it is not a product.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mock_bank import data

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Mock Bank Back Office")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_SUBACCOUNT_MIN_DEPOSIT = 25.00
_opened_subaccounts: list[dict] = []  # in-memory only, for the (unused-by-replay) confirm step


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "search.html", {"error": None})


@app.get("/members/search", response_class=HTMLResponse)
def search(request: Request, member_id: str = ""):
    member_id = member_id.strip()

    if not member_id.isdigit():
        return templates.TemplateResponse(
            request, "search.html", {"error": "Member ID must contain only digits."}
        )

    if member_id == data.HARD_FAILURE_MEMBER_ID:
        # Simulate an outright application error (not a graceful business
        # outcome) -- the kind of thing replay must classify as hard_failure.
        raise RuntimeError("simulated core banking outage")

    if data.get_member(member_id) is None:
        return templates.TemplateResponse(
            request, "search.html", {"error": f"No member found with ID {member_id}."}
        )

    return RedirectResponse(url=f"/members/{member_id}", status_code=303)


@app.get("/members/{member_id}", response_class=HTMLResponse)
def member_detail(request: Request, member_id: str):
    member = data.get_member(member_id)
    if member is None:
        return templates.TemplateResponse(
            request, "search.html", {"error": f"No member found with ID {member_id}."}
        )

    if data.is_restricted(member_id):
        return templates.TemplateResponse(
            request,
            "member_detail.html",
            {"member_id": member_id, "member": None, "restricted": True, "notice": False},
        )

    if data.should_be_slow(member_id):
        # Deliberately longer than a recorded step's default 15000ms timeout,
        # so replaying this scenario genuinely exercises PlaywrightSurface's
        # transient-load recovery path rather than just waiting it out.
        time.sleep(17)

    if data.should_show_interstitial(member_id):
        return templates.TemplateResponse(
            request,
            "member_detail.html",
            {"member_id": member_id, "member": None, "restricted": False, "notice": True},
        )

    return templates.TemplateResponse(
        request,
        "member_detail.html",
        {"member_id": member_id, "member": member, "restricted": False, "notice": False},
    )


@app.post("/members/{member_id}/dismiss")
def dismiss_notice(member_id: str):
    data.mark_interstitial_dismissed(member_id)
    return RedirectResponse(url=f"/members/{member_id}", status_code=303)


@app.get("/members/{member_id}/sub-account/new", response_class=HTMLResponse)
def sub_account_new(request: Request, member_id: str):
    member = data.get_member(member_id)
    return templates.TemplateResponse(
        request, "sub_account_new.html", {"member_id": member_id, "member": member, "error": None}
    )


@app.post("/members/{member_id}/sub-account/review", response_class=HTMLResponse)
def sub_account_review(request: Request, member_id: str, account_type: str = Form(...), initial_deposit: str = Form("")):
    member = data.get_member(member_id)
    try:
        amount = float(initial_deposit)
    except ValueError:
        amount = -1.0

    if amount < _SUBACCOUNT_MIN_DEPOSIT:
        return templates.TemplateResponse(
            request,
            "sub_account_new.html",
            {
                "member_id": member_id,
                "member": member,
                "error": f"Initial deposit must be at least ${_SUBACCOUNT_MIN_DEPOSIT:.2f}.",
            },
        )

    return templates.TemplateResponse(
        request,
        "sub_account_review.html",
        {"member_id": member_id, "member": member, "account_type": account_type, "initial_deposit": f"{amount:.2f}"},
    )


@app.post("/members/{member_id}/sub-account/confirm", response_class=HTMLResponse)
def sub_account_confirm(request: Request, member_id: str, account_type: str = Form(...), initial_deposit: str = Form(...)):
    # Deliberately never targeted by any recorded capability in this project
    # (see REPORT.md, Safety) -- included only so the write-up can point at a
    # real irreversible endpoint when discussing the policy engine's handling
    # of risky/irreversible actions.
    _opened_subaccounts.append({"member_id": member_id, "account_type": account_type, "initial_deposit": initial_deposit})
    return templates.TemplateResponse(
        request, "sub_account_confirmed.html", {"member_id": member_id, "account_type": account_type}
    )


@app.post("/admin/reset")
def admin_reset():
    data.reset_scenarios()
    return {"ok": True}
