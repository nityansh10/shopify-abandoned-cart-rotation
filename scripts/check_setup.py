"""Pre-flight check: proves the Shopify token and both Google Sheets work.

    python scripts/check_setup.py

Writes nothing to the sheets beyond creating the header row if the tab is empty.
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from src.shopify_client import ShopifyClient, ShopifyError  # noqa: E402
from src.sheets_client import SheetsClient, SheetsError, service_account_email  # noqa: E402

OK, BAD = "[ OK ]", "[FAIL]"


def main():
    load_dotenv()
    failures = 0

    print("\n--- Shopify ---")
    try:
        shopify = ShopifyClient()
        print("{} store            {}".format(OK, shopify.domain))
        print("{} shop name        {}".format(OK, shopify.shop_name()))
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=7)
        leads = shopify.fetch_abandoned_checkouts(since)
        print("{} abandoned carts  {} in the last 7 days (mode: {})".format(
            OK, len(leads), shopify.mode))
        for lead in leads[:3]:
            print("       - {} | {} | {} {} | {}".format(
                lead["name"] or lead["checkout_id"],
                lead["email"] or lead["phone"] or "no contact",
                lead["total_price"], lead["currency"],
                lead["items"][:60]))
        if leads and not any(l["email"] or l["phone"] for l in leads):
            print("       ! none of them carry an email or phone - if you are on "
                  "SHOPIFY_API_MODE=graphql, try rest")
    except ShopifyError as exc:
        failures += 1
        print("{} {}".format(BAD, exc))

    print("\n--- Google Sheets ---")
    print("service account: {}".format(service_account_email() or "(unknown)"))
    try:
        with open(os.environ.get("CONFIG_PATH", "config.json"), "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        sheets = SheetsClient()
        for rep in cfg.get("reps", []):
            name, sheet_id = rep.get("name"), rep.get("sheet_id", "")
            if "PASTE_" in sheet_id:
                failures += 1
                print("{} {:<10} sheet_id is still a placeholder in config.json".format(BAD, name))
                continue
            try:
                ws = sheets.worksheet(sheet_id, rep.get("worksheet", "Leads"))
                existing = sheets.existing_checkout_links(ws)
                print("{} {:<10} '{}' tab reachable, {} lead(s) already in it".format(
                    OK, name, ws.title, len(existing)))
            except SheetsError as exc:
                failures += 1
                print("{} {:<10} {}".format(BAD, name, exc))
    except SheetsError as exc:
        failures += 1
        print("{} {}".format(BAD, exc))

    print("\n{}".format("All good - you can run `python main.py`." if not failures
                        else "{} problem(s) above need fixing.".format(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
