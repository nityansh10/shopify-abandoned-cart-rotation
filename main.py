"""Shopify abandoned carts -> Suman's sheet, Priyanka's sheet, alternating.

Every run:
  1. pull abandoned checkouts from Shopify
  2. drop the ones already sent, the ones too fresh to count as abandoned,
     and (optionally) the ones with no way to contact the customer
  3. hand out what is left strictly in turn - 1st to rep A, 2nd to rep B, 3rd to rep A ...
  4. append to each rep's Google Sheet and remember the rotation position

Run locally with:  python main.py            (add DRY_RUN=1 to write nothing)
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.shopify_client import ShopifyClient, ShopifyError
from src.sheets_client import SheetsClient, SheetsError, COLUMNS
from src.state import State

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("abandoned-cart-sync")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    reps = [r for r in cfg.get("reps", []) if r.get("name") and r.get("sheet_id")]
    if not reps:
        raise SystemExit("config.json has no usable reps (each needs a name and a sheet_id)")

    unset = [r["name"] for r in reps if "PASTE_" in r["sheet_id"]]
    if unset and (_flag("DRY_RUN") or _flag("SEED_EXISTING")):
        log.warning("sheet_id still a placeholder for: %s (fine for this mode)", ", ".join(unset))
    elif unset:
        raise SystemExit(
            "Still a placeholder sheet_id in config.json for: {}. Paste the real "
            "spreadsheet ID (the long bit in the sheet URL between /d/ and /edit).".format(
                ", ".join(unset))
        )

    cfg["reps"] = reps
    return cfg


def parse_shopify_time(value):
    """Shopify sends ISO 8601; normalise Z to +00:00 and always return aware UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local(dt, tz):
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else ""


def build_row(lead, rep_name, tz, now):
    """One sheet row, in COLUMNS order."""
    row = [
        local(parse_shopify_time(lead["created_at"]), tz),  # when the cart was abandoned
        lead["customer_name"],
        lead["phone"],
        lead["email"],
        lead["total_price"],
        lead["recovery_url"],
    ]
    assert len(row) == len(COLUMNS), "build_row is out of sync with COLUMNS"
    return row


def select_new_leads(leads, cfg, state, already_in_sheets, now):
    """Filter to genuinely new, genuinely abandoned carts, oldest first."""
    min_age = timedelta(minutes=int(cfg.get("min_age_minutes", 30)))
    kept, skipped = [], {"seen": 0, "too_fresh": 0, "completed": 0, "no_contact": 0}

    for lead in leads:
        checkout_id = lead["checkout_id"]
        if not checkout_id:
            continue
        if state.is_processed(checkout_id) or lead["recovery_url"] in already_in_sheets:
            skipped["seen"] += 1
            continue
        if lead.get("completed_at"):
            skipped["completed"] += 1
            continue

        created = parse_shopify_time(lead["created_at"])
        if created and now - created < min_age:
            skipped["too_fresh"] += 1
            continue
        if cfg.get("require_contact", True) and not (lead["email"] or lead["phone"]):
            skipped["no_contact"] += 1
            continue

        kept.append(lead)

    kept.sort(key=lambda l: parse_shopify_time(l["created_at"]) or now)

    log.info("Skipped: %d already sent, %d younger than %s, %d completed, %d with no email/phone",
             skipped["seen"], skipped["too_fresh"], min_age, skipped["completed"],
             skipped["no_contact"])
    return kept[:int(cfg.get("max_leads_per_run", 200))]


def main():
    load_dotenv()
    cfg = load_config()
    reps = cfg["reps"]
    tz = ZoneInfo(cfg.get("timezone", "Asia/Kolkata"))
    now = datetime.now(timezone.utc)
    dry_run = _flag("DRY_RUN")
    seed = _flag("SEED_EXISTING")

    log.info("Rotation order: %s", " -> ".join(r["name"] for r in reps))

    shopify = ShopifyClient()
    created_at_min = now - timedelta(days=int(cfg.get("lookback_days", 7)))
    leads = shopify.fetch_abandoned_checkouts(created_at_min)

    state = State()
    log.info("Next in line: %s (rotation pointer %d)",
             reps[state.next_index % len(reps)]["name"], state.next_index)

    if seed:
        # One-off: swallow the existing backlog so the team only ever sees carts
        # abandoned from now on.
        fresh = 0
        for lead in leads:
            if lead["checkout_id"] and not state.is_processed(lead["checkout_id"]):
                state.processed[lead["checkout_id"]] = now.isoformat()
                fresh += 1
        state.save()
        log.info("SEED_EXISTING: marked %d existing cart(s) as already handled. "
                 "Only carts abandoned after now will be assigned.", fresh)
        return 0

    sheets = None
    worksheets, already_in_sheets = {}, set()
    if not dry_run:
        sheets = SheetsClient()
        for rep in reps:
            ws = sheets.worksheet(rep["sheet_id"], rep.get("worksheet", "Leads"))
            worksheets[rep["name"]] = ws
            already_in_sheets |= sheets.existing_checkout_links(ws)
    else:
        log.info("DRY_RUN: skipping Google Sheets entirely (no duplicate check against them).")

    new_leads = select_new_leads(leads, cfg, state, already_in_sheets, now)
    if not new_leads:
        log.info("No new abandoned carts to assign.")
        state.save()
        return 0

    # Hand them out strictly in turn.
    batches = {rep["name"]: [] for rep in reps}
    assignments = []
    for lead in new_leads:
        rep = state.take_turn(reps)
        batches[rep["name"]].append(build_row(lead, rep["name"], tz, now))
        assignments.append((lead, rep["name"]))
        log.info("  %s  %s  %s  ->  %s", lead["name"] or lead["checkout_id"],
                 lead["email"] or lead["phone"], lead["total_price"], rep["name"])

    if dry_run:
        log.info("DRY_RUN set - nothing written to Google Sheets, rotation not saved.")
        return 0

    written = set()
    for rep in reps:
        rows = batches[rep["name"]]
        if not rows:
            continue
        sheets.append(worksheets[rep["name"]], rows)
        log.info("Wrote %d row(s) to %s's sheet", len(rows), rep["name"])
        written.add(rep["name"])

    for lead, rep_name in assignments:
        if rep_name in written:
            state.mark_processed(lead["checkout_id"], rep_name)
    state.save()

    log.info("Done. Lifetime totals: %s",
             ", ".join("{}={}".format(k, v) for k, v in sorted(state.assigned_count.items()))
             or "none yet")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ShopifyError, SheetsError) as exc:
        log.error("%s", exc)
        sys.exit(1)
