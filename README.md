## Windows one-click app (no commands after download)

GitHub Actions builds a **Windows `Roost.exe`** from source whenever the main branch changes. Open the repository **Actions → Build Windows Roost.exe → latest successful run → Artifacts → Roost-Windows**, extract the EXE, and double-click it. Windows may display a SmartScreen warning for an unsigned self-built executable. The build needs to complete successfully before an EXE is available; the ZIP source by itself is not a Windows binary.

All personal settings and saved searches persist at **`%USERPROFILE%\Downloads\RoostData\`**:

- `RoostData\.env` — API and SMTP credentials. Never commit or share this file.
- `RoostData\scout.db` — listings, saved searches, favorites, email history, and API counters.

On first launch, Roost copies `.env` and `scout.db` from the old project folder **only if they are in the process working directory or next to the executable**, or directly in Downloads. To migrate from an unrelated old location, copy both files yourself to `Downloads\RoostData` before opening the EXE. The files are never intentionally overwritten by a new build. Keep a backup before moving data.

Roost binds to `127.0.0.1:8000` (your computer only) and opens its dashboard in your default browser. **The EXE needs to remain running to send background email alerts.** To stop it, close its process from Task Manager. Do not run two copies simultaneously. If you are running the older PowerShell server, stop it before launching the EXE.

# Roost — NYC Apartment Radar

A private, locally runnable NYC apartment listing aggregator with saved searches, conditional SMTP emails, a transit commute cap, free-tier StreetEasy data via API.SH and personal saved-search emails, Reddit API integration (only with approved access), configurable permitted RSS/Atom feeds, authorized JSON listing feeds, and CSV imports.

## Start

```bash
cd apt-scout
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # On Windows: copy .env.example .env
uvicorn server:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Use **Settings → Load sample listings** to explore the interface without provider credentials. Examples are explicitly tagged and never sent by email.

## Configure real integrations

Edit `.env`, restart the server, then **Sources & integrations → Check for new listings**.

- **StreetEasy via API.SH (recommended free source):** Sign up at https://api.sh/streeteasy-api and copy the free API key into `APISH_KEY`. The provider currently advertises **1,000 free credits/month**, with each search costing one credit (as of November 2026). Roost requests up to 200 newest listings (first page only, `sort=newest`, `fresh=1` to bypass 24-hour caching) per configured borough, default Manhattan, Brooklyn and Queens; each three-borough check costs **3 credits**, so a **180-minute** interval is ~720 credits in a 30-day month. A **900-request local monthly cap** is enforced across automatic and manual syncs. Configure `APISH_AREAS`, `APISH_INTERVAL_MINUTES` and `APISH_MONTHLY_BUDGET` to trade speed for coverage, never exceed 950 local monthly requests. This does **not** guarantee that the provider will remain free, supply up-to-date listings, or grant redistribution rights; review API.SH terms and StreetEasy terms before use. Because Roost only requests the first page per borough, this is *not* exhaustive coverage. A third-party API is not an official StreetEasy integration. Account/key creation is required.
- **StreetEasy saved-search emails (optional, free fallback):** StreetEasy explicitly supports emails on new matches. Create saved searches at https://streeteasy.com/for-rent/nyc , enable notifications, and enter the **IMAP_HOST, IMAP_USER and IMAP_PASSWORD** of the inbox receiving those emails. Roost reads up to 100 recent emails from the past 14 days, accepts only `@streeteasy.com`-family sender domains, parses links that directly identify StreetEasy listings, and marks processed message UIDs locally. It does not access or scrape StreetEasy webpages. Email frequency is controlled by StreetEasy, possibly up to three times daily even on “instant”; this method is **not minute-by-minute**. This is a convenience adapter and may need adjustment to future StreetEasy email templates. Use an app-specific email password, never your primary mailbox password. StreetEasy email listings may lack a price, bedrooms, or exact address and therefore may not pass strict rent/commute filters.
- **Reddit:** Set `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, and your descriptive `REDDIT_USER_AGENT`. Reddit may require explicit approval of API access, and commercial purposes require separate agreement. Reads the recent posts of `r/NYCapartments`, `r/nyc_sublets`, `r/LeaseTakeoverNYC`, `r/NYCroommates`, `r/subletting` if available/authorized. Posts are heuristic; price/bedroom values are inferred from text. Do not assume every post is a real listing. Follow Reddit's content deletion and retention rules; this starter app does *not* yet implement deleted-post reconciliation and should not be treated as production-compliant Reddit archival software.
- **RSS/Atom:** Add RSS URLs explicitly permitted for your intended use. This is not a workaround for a restricted site and does not magically create RSS support for websites that do not offer it. Some publishers have no feeds.
- **Licensed listing feed:** Set `LICENSED_LISTING_FEED_URL` pointing to your authorized HTTPS JSON array (or `{ "listings": [...] }`) and optionally `LICENSED_LISTING_FEED_BEARER`. Field schema listed below.
- **Manual import:** CSV with `title,url` required, optional `description,price,beds,borough,neighborhood,address,lat,lng,image,posted,no_fee,laundry,washer_dryer,pet_friendly,kind`. `posted` should be Unix seconds. Do not import or redistribute content without the rights to do so.
- **Other sites:** Zillow / RentHop / Leasebreak / Listings Project are not automatically scraped; use permitted feeds, approved APIs, or personal exports. Roost does not bypass login, robots, or anti-bot controls.

## Transit commute filter — Google Routes API with fail-closed local budgets

Roost now uses **Routes API v2 `computeRoutes`** with `travelMode=TRANSIT` and a weekday arrival target (default 9:00 AM America/New_York). It no longer calls Directions API (Legacy) or Geocoding API. Routes accepts apartment addresses/coordinates and the destination address directly. Do not enable extra Google APIs for this project unless you deliberately add code for them.

1. In Google Cloud, enable **Routes API** for a project with billing enabled, and generate a backend API key restricted to **Routes API**. Keep this key in your local `.env` only (never commit it or paste it into a chat).
2. Configure:

```env
GOOGLE_MAPS_API_KEY=your_new_key
GOOGLE_ROUTES_MONTHLY_LIMIT=4000
GOOGLE_ROUTES_DAILY_LIMIT=100
```

3. Restart Roost. Open **Settings** to see the UTC day/month quota counters. To disable routing entirely, set `GOOGLE_ROUTES_DAILY_LIMIT=0` (or remove the API key).

### Strict Roost limits

- The US free allowance for **Routes: Compute Routes Pro** is **5,000 requests/month** at time of writing (Nov 2026); transit is a Pro feature. Roost uses a more conservative built-in ceiling of **4,000 requests per UTC month and 100 per UTC day**. If you lower the limits in `.env`, they take effect; if you try to raise them above these fixed built-in ceilings, they are clamped. Invalid settings fail closed and disable calls.
- Every outgoing request must first reserve a usage slot in the shared `scout.db` SQLite database using an atomic `BEGIN IMMEDIATE` transaction. **Slots are consumed even when a request fails or times out**, reducing surprise billing. Concurrent browser loads, alerts and background polling share the same counters even across restarts, as long as they use the same database.
- Successful apartment/destination/arrival-hour route results are cached for **24 hours**; cache hits do not spend a slot. Listings without an address or coordinates are excluded by the strict commute filter and do not spend a slot.
- Once either budget is exhausted, Roost makes **no further transit API requests** until the relevant UTC daily/monthly period changes. Unknown routes are excluded from searches with a commute cap; other listing search features continue working.
- Counters track requests initiated by **this Roost instance and its shared database only**, not other projects, keys, deployments, or manual calls. **Do not delete `scout.db`** if you want counts to persist. Do not run separate installations with the same Google project/key and separate databases.
- Google pricing, SKU classification, and billing mechanics may change; these app-side limits **do not guarantee a $0 Google bill**. External requests, free tier reductions, extra enabled APIs or a different billing account can still incur charges. For additional safety, restrict the key to Routes API, set provider-side quotas and billing alerts, and monitor actual billing in Google Cloud. Budget alerts alone do not block charges.

This implementation does not call Google Geocoding, Map Tiles, Places, or Route Matrix. The returned transit duration includes transit and associated walking as calculated by Google. Transit estimates depend on departure/arrival schedule and may be unavailable for a listing with insufficient address details.

## Email alerts

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, and `ALERT_TO`. STARTTLS defaults on. Save a search with alerts checked. The worker polls sources every `POLL_MINUTES` (15 default, minimum 5) while the server is running. An email goes out for newly ingested listings that satisfy the filters and can be transit-routed. On the very first source sync, old listings are marked seen to avoid a backlog of stale alerts. Existing items are also marked seen when you create a new saved search. Emails are marked sent only after SMTP accepts the message (which doesn't guarantee inbox delivery). The actual delay depends on sources, rate limits, and your polling interval.

## Important limitations / production hardening

1. **This is a functional starter, not a hosted service.** No provider credentials are preconfigured, there is no official StreetEasy connection, and real-time listing arrival is not guaranteed.
2. **Security:** Bind to `127.0.0.1` only unless deploying behind authenticated HTTPS; the app is otherwise single-user and exposes settings/listings operations. `ADMIN_TOKEN` protects API calls, but is not a substitute for actual web authentication in a public deployment.
3. **SSRF:** RSS and authorized feed URLs should be restricted with an allowlist and DNS/IP validation before untrusted users can enter URLs. Local-only use is assumed.
4. **Reddit compliance:** Handle edited/removed/deleted posts, permitted retention, rate-limit headers, user consent, and developer terms before production use.
5. **Parsing:** Reddit and RSS listings often lack precise address, room count, or trustworthy advertised rent. Extracted price / tags are hints, not verified facts. Verify originals and watch for scams.
6. **Coverage:** Some sources, including Facebook groups, cannot be responsibly auto-integrated without authorized data access. API.SH is an independent third-party service, not an endorsement or license from StreetEasy. This app intentionally does not attempt anti-bot evasion.
7. **Alerts are on new ingests**, not changed prices or edited postings. Future improvements include edit reconciliation, listing deduplication across sites and separate searches for favorite-only items.
8. **Reddit pagination:** First page of newest posts only. No backfilling/advanced pagination.

## Testing

`python -m unittest discover -s tests -v` runs parser, quota and StreetEasy integration tests with mocked provider responses; it never requires API credentials.

- In Settings, click **Load sample listings**. Turn off the commute filter (since demo posts have no exact address), enable **Show example listings**, and click **Show matching homes**.
- Save a search and inspect **Saved searches**.
- CSV import a row with a verifiable public listing URL to test real listing ingestion.
- Enable SMTP and press **Send a test email**. It sends a dummy test, not an apartment claim.

## Data schema for authorized feed

```json
[
  { "id": "my-feed:123", "title": "1BR in Astoria", "description": "No fee, laundry", "url": "https://example.org/listings/123", "price": 2700, "beds": 1, "borough": "Queens", "neighborhood": "Astoria", "address": "Example address, Queens, NY", "posted": 1794900000, "no_fee": true, "washer_dryer": false, "kind": "rental" }
]
```

Only use a real address and data licensed or verified for your own authorized feed. This example schema is explanatory, not a live listing.


## Map view (new)

In **Discover**, use the **List / Map** toggle above the search results. The map plots matching apartments with valid lat/lng coordinates from listing sources, shows rent-price markers, and opens listing previews linking back to their original websites. Every normal search filter applies to both views. A counter explains how many matching listings cannot be pinned because the upstream source did not supply usable coordinates. No coordinates are guessed and no paid Google request is made by the map itself.

Map view uses open-source Leaflet and OpenStreetMap public raster tiles, downloaded in the browser. Internet access is required to display tiles. Respect the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/) if hosting for other users or adding heavy traffic; production traffic may require a dedicated tile provider or self-hosting. OpenStreetMap attribution appears on the map. Transit time filtering is separate and still requires the configured Google Routes integration and its existing hard limits.

**Updating an existing installation:** Extract the ZIP, copy the new `server.py`, `static/`, and other source files into your existing `apt-scout` folder, but preserve the old `.env` and `scout.db` files. Stop the server with Ctrl+C, restart with `python -m uvicorn server:app --reload`, and hard-refresh Chrome (Ctrl+Shift+R).


## Update without losing settings (Windows)

Keep a single `apt-scout` folder. Stop the running server (Ctrl+C), download the next trusted Roost ZIP to Downloads, and run this command **in your existing `apt-scout` folder**:

```powershell
.\update-roost.ps1 -ZipPath "$HOME\Downloads\roost-nyc-map-view.zip"
```

Change the ZIP name to the new downloaded release. If script execution is blocked, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\update-roost.ps1 -ZipPath "$HOME\Downloads\roost-nyc-map-view.zip"
```

The updater backs up `.env` and `scout.db` in `backups/<timestamp>/`, replaces only known application files, updates Python dependencies, and leaves `.env`, `scout.db`, and `.venv` untouched. Saved searches, favorites, alert history, provider counters, and email credentials remain in those files. Do not run the updater while Roost is running. Restart the server after updating. The server must remain running for scheduled checks/emails; it cannot catch up on checks missed during downtime unless implemented explicitly. Only run ZIPs from a source you trust.
