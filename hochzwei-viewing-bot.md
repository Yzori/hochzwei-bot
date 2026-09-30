# Hochzwei Luzern: viewing sign-up bot

Goal: watch https://hochzwei-luzern.ch/wohnungen/ for **4.5-room** apartments and book a
**viewing** slot (Besichtigung) the moment one opens. Viewings fill up within minutes, so speed
matters. This is not the full rental application, just booking a viewing slot.

Researched 2026-09-30 by reading the site and the MyWincasa front-end JS. Not built yet.

## How the site works

### 1. Listing page: hochzwei-luzern.ch/wohnungen/
- Static HTML table, so plain `requests` + BeautifulSoup works, no headless browser needed.
- Columns: unit number (e.g. `02696.01.2102`), rooms (2.5 / 3.5 / 4.5 Zimmer), floor, m²,
  gross rent, status ("Frei").
- Each row links to:
  - Viewing: `https://www.mywincasa.ch/viewing/show/{unitNumber}`
  - Application (not needed here): `https://www.mywincasa.ch/candidate/{uuid}`

### 2. MyWincasa: Angular app backed by a public JSON API
- Config: `https://www.mywincasa.ch/assets/app.config.json`
- Base URL: `https://api.wincasa.ch/fn-crm-ext-prd-chn/v1/`
- No auth: the only header sent is `Content-Type: application/json`. No captcha found in the JS.
- Viewing code lives in the lazy chunk `chunk-PRYAVVOT.js` (the filename changes on every deploy).

#### Check viewing status
`GET {base}vermietung-by-reference/{unitNumber}`

Real response, 2026-09-30, unit 02696.01.2102 (4.5 rooms, floor 21, 92 m², CHF 2,390):
```json
{
  "id": "9169c519-68b6-ed11-b597-000d3a831ba9",
  "referenceNumber": "02696.01.2102",
  "hasViewingAppointments": false,
  "viewingsFrom": "01.10.2026 00:00",
  "rentalUnit": { "fullAddress": "Zihlmattweg 44, 6005 Luzern" },
  "viewingAppointments": [],
  "viewingBy": "TENANT",
  "viewingState": "FULLY_BOOKED"
}
```
- `viewingState` seen so far: `FULLY_BOOKED`. Other values are unknown; log whatever comes back.
- When slots are open, each one should appear in `viewingAppointments` with an `appointmentId`.
  **Not seen yet.** Log the first non-empty response to confirm the field names.
- Other GET endpoints in the JS: `vermietung/{id}`, `besichtigungstermine/{id}` (404s when
  called with a unit number), `rental-object-by-waitlist-entry/{id}`.

#### Book a slot
`POST {base}appointment-signup/{appointmentId}`
```json
{ "appointmentId": "...", "email": "...", "firstName": "...",
  "lastName": "...", "phone": "...", "language": "de" }
```

#### Waitlist fallback (when fully booked)
`POST {base}waitlist-signup/{viewingId}`
```json
{ "viewingId": "...", "email": "...", "language": "de" }
```
`viewingId` is probably the `id` from the status response. Unconfirmed.

#### Other endpoints
`cancel-appointment`, `cancel-waitlist-subscription`

## Bot design
1. Every 1–2 min: fetch the listing and pick out the rows with 4.5 rooms.
2. For each one: `GET vermietung-by-reference/{unit}`.
3. If `viewingAppointments` isn't empty, pick the earliest slot that fits my availability and POST
   `appointment-signup`. Book **one** slot per unit.
4. If the unit is fully booked, join the waitlist once.
5. Push-notify my phone (ntfy.sh is simplest) with the unit, time and result.
6. Keep a `state.json` file of units already booked or waitlisted, so nothing happens twice.

Hosting: Windows Task Scheduler (the PC must be on), or GitHub Actions on a cron schedule (free,
runs while the PC is off; minimum interval is about 5 min and runs can start late).

## Open items / caveats
- The booking POST is taken from the JS, not tested live. Watch the first real booking, or start
  in notify-only mode.
- Wincasa can add a captcha or rate limit at any time. Keep polling gentle (at least 60 s apart).
- Details needed: name, email, phone, available time windows, notification channel.
