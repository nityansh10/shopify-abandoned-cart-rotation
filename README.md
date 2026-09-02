# Abandoned cart → sales team round-robin

Every 5 minutes GitHub Actions asks Shopify for abandoned checkouts and drops each new
one into a sales rep's Google Sheet, **strictly alternating**:

```
cart #1 → Suman      cart #3 → Suman      cart #5 → Suman
cart #2 → Priyanka   cart #4 → Priyanka   cart #6 → Priyanka
```

It runs on GitHub's servers, so nobody's laptop needs to be on. The rotation pointer
lives in `state/state.json`, which the workflow commits back after every run — so the
turn order survives across runs, restarts, and months of downtime.

---

## What each rep sees

| Assigned At | Assigned To | Checkout ID | Checkout | Abandoned At | Customer Name | Email | Phone | City | State | Country | Products | Items | Cart Value | Currency | Recovery Link | Call Status | Remarks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

`Recovery Link` is Shopify's own checkout-recovery URL — the rep can send it to the
customer and the cart comes back exactly as it was. `Call Status` and `Remarks` are left
empty for the rep to fill in; the script never touches existing rows.

---

## Setup

### 1. Shopify — already done ✅

Your `.env` has `SHOPIFY_STORE_URL` and `SHOPIFY_ACCESS_TOKEN`.

One thing to confirm in the Shopify admin (**Settings → Apps and sales channels →
Develop apps → your app → Configuration → Admin API integration**):

- scope **`read_orders`** is ticked
- under **Protected customer data access**, request access and tick **Name, Email,
  Phone, Address** — without this Shopify blanks out the customer details

If you change scopes, reinstall the app and copy the new `shpat_` token.

### 2. Two Google Sheets

Create one spreadsheet per rep (e.g. *Abandoned Cart Leads — Suman* and
*… — Priyanka*). Leave them empty; the script writes the header row itself.

Copy each spreadsheet's ID from its URL:

```
https://docs.google.com/spreadsheets/d/1AbCdEf...THIS_PART...XyZ/edit
```

and paste them into `config.json`.

### 3. A Google service account (so the robot can write)

1. <https://console.cloud.google.com/> → create a project (any name).
2. **APIs & Services → Library** → enable **Google Sheets API** *and* **Google Drive API**.
3. **APIs & Services → Credentials → Create credentials → Service account** → name it
   `abandoned-cart-bot` → Done.
4. Open the service account → **Keys → Add key → Create new key → JSON**. A `.json`
   file downloads.
5. Copy the `client_email` from that file (looks like
   `abandoned-cart-bot@your-project.iam.gserviceaccount.com`) and **share both
   spreadsheets with it as Editor**, exactly like sharing with a person.

### 4. Local test (optional but recommended)

Add the JSON key to `.env` as one line:

```
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account", ... }
```

Then:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python scripts\check_setup.py     # proves both sides work
.\.venv\Scripts\python main.py                    # real run
```

`DRY_RUN=1` prints who *would* get what and writes nothing — it doesn't even need the
Google credentials, so you can sanity-check the rotation before setting up the sheets:

```powershell
$env:DRY_RUN=1; .\.venv\Scripts\python main.py
```

### 4a. Decide what happens to the existing backlog

Your store currently has **~136 abandoned carts in the last 7 days** (roughly 20 a day).
On the very first real run all of them get split between the two sheets at once —
68 each. If you'd rather start clean and only work carts abandoned from today onward:

```powershell
$env:SEED_EXISTING=1; .\.venv\Scripts\python main.py; $env:SEED_EXISTING=""
```

That marks everything currently open as already handled without writing a single row.
Commit the updated `state/state.json` before pushing, and the team starts fresh.

### 5. Push to GitHub

```powershell
git init
git add .
git commit -m "Abandoned cart round-robin sync"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Make the repo **private** — the sheets IDs and rep names live in it.
`.env` is git-ignored, so no secrets are pushed.

### 6. Add the three GitHub secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `SHOPIFY_STORE_URL` | `fedus-…myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | the `shpat_…` token |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the entire service-account JSON file, pasted as-is |

Then **Actions → Abandoned cart sync → Run workflow** to fire the first run by hand.
After that it runs itself every 5 minutes.

> The workflow also needs write access to commit the rotation state:
> **Settings → Actions → General → Workflow permissions → Read and write permissions**.

---

## Tuning (`config.json`)

| Setting | Default | Meaning |
|---|---|---|
| `reps` | Suman, Priyanka | Rotation order. Add a third person and it becomes a 3-way rotation automatically. |
| `min_age_minutes` | `30` | How long a checkout must sit untouched before it counts as abandoned. Stops a rep phoning someone who is still typing their card number. |
| `lookback_days` | `7` | How far back each run looks. Bigger = more safety net if the workflow was paused. |
| `require_contact` | `true` | Skip carts with neither email nor phone (a rep can't do anything with them). Set `false` to capture everything. |
| `timezone` | `Asia/Kolkata` | Timezone for the timestamps written into the sheets. |
| `max_leads_per_run` | `200` | Cap per run, so a backlog can't blow through Google's write quota. |

Changing the cron: edit `.github/workflows/abandoned-cart-sync.yml`. 5 minutes is
GitHub's shortest allowed interval, and the scheduler is best-effort on shared runners,
so `*/5` can drift by a few minutes at busy times.

**Actions minutes:** every run bills at least 1 minute. `*/5` is ~288 runs/day ≈ 8,600
minutes/month, far past the 2,000 free minutes a **private** repo gets. Either make the
repo **public** (Actions is free and unlimited there — the token and the Google key stay
in encrypted secrets, so nothing sensitive is exposed) or keep it private and raise the
interval to `*/30`, which fits inside the free tier.

Note that `min_age_minutes` also caps how fast a lead can appear: at the default `30`,
polling every 5 minutes still means a cart surfaces ~30 minutes after it was abandoned.
Lower it to `10`–`15` if you want the team on the phone sooner.

---

## How duplicates are prevented

Two independent guards, so a lead is never sent twice:

1. `state/state.json` remembers every checkout ID already assigned (60 days).
2. Before writing, the script reads the `Checkout ID` column from both sheets and skips
   anything already there — so even if the state file were deleted, nothing repeats.

If the sheet write fails, the checkout is **not** marked as processed, so the next run
retries it and the rotation position is not consumed.

## Resetting or rebalancing the rotation

Edit `state/state.json` and commit:

- `next_index: 0` → Suman gets the next lead, `1` → Priyanka
- clearing `processed` makes the last 7 days of carts eligible again (they will still be
  blocked by the sheet-level duplicate check unless you clear the sheets too)
- `assigned_count` is just a running tally per rep, handy for a fairness check

`SEED_EXISTING=1 python main.py` re-swallows whatever is currently open — useful after
a long pause when you don't want a week of stale carts landing on the team at once.

## If something looks wrong

| Symptom | Cause |
|---|---|
| `Shopify rejected the token (HTTP 401/403)` | Token wrong, or `read_orders` / protected customer data not granted. |
| Rows arrive but Email and Phone are blank | Protected customer data access not approved in the Shopify app. |
| `Cannot open spreadsheet …` | The sheet isn't shared with the service account's `client_email`, or the ID is wrong. |
| Nothing ever appears | Normal if there are no carts older than `min_age_minutes`; check the Actions run log, it prints exactly what it skipped and why. |
| Both people got the same lead | Shouldn't happen — the workflow uses a concurrency group so runs never overlap. Check for a second workflow file. |
