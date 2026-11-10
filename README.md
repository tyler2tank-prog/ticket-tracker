# Ticket Price Tracker

Watches ticket prices on **SeatGeek** (resale: lowest, median and average price, plus how many
listings are up) and **Ticketmaster** (face-value price ranges) for the events in
`watchlist.yaml`. It saves a snapshot on a schedule that speeds up as each event gets closer,
then builds a chart page showing how prices moved.

| Time until event | How often it checks |
|---|---|
| 30+ days | once a day |
| 7–30 days | every 6 hours |
| 2–7 days | every 3 hours |
| 8 hours – 2 days | every hour |
| final 8 hours | every 30 minutes |

Every snapshot is saved to `data/snapshots.csv`, so you can also open the raw data in Excel or Google Sheets.

---

## 1. Get your two free API keys (about 10 minutes)

**SeatGeek**
1. Go to https://seatgeek.com/account/develop and sign in (any SeatGeek account works).
2. Click **New App** and fill in a name and website (your GitHub link is fine).
3. Copy the **Client ID** (and the **Client Secret**, if it shows one).

**Ticketmaster**
1. Go to https://developer.ticketmaster.com and click **Get your API key** to make an account.
2. Open **My Apps**. A default app already exists. Copy its **Consumer Key**: that's your API key.

## 2. Run it free on GitHub, 24/7

1. Make a free account at https://github.com and create a **new repository** (e.g. `ticket-tracker`).
2. Upload every file in this folder to it, including the hidden `.github` folder.
   (Easiest way: on the repo page, click **Add file → Upload files** and drag the whole folder in.)
3. In the repo, open **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `SEATGEEK_CLIENT_ID`
   - `SEATGEEK_CLIENT_SECRET` (skip if SeatGeek didn't give you one)
   - `TICKETMASTER_API_KEY`
4. Open the **Actions** tab, click **Track ticket prices**, then **Run workflow** to start it now.
   After that it runs by itself every 30 minutes.

**To see your charts:** open `docs/index.html` in the repo and download it, or turn on
**Settings → Pages → Source: Deploy from branch → `main` / `docs`** to get a live link that updates
on every run. (Pages is free for public repos. Ticket prices aren't private data, so making the repo public is fine.)

## 3. Or run it on your own computer

```bash
pip install -r requirements.txt
cp .env.example .env          # then paste your keys into .env
python tracker.py check       # tests your keys and shows what each search finds
python tracker.py poll        # takes a snapshot
python tracker.py report      # builds docs/index.html
```

Your computer has to be on for this to keep collecting, which is why GitHub is the better option.

## Commands

| Command | What it does |
|---|---|
| `python tracker.py check` | Tests your keys and lists the events each search finds |
| `python tracker.py poll` | Snapshots every event that's due (add `--force` to snapshot all of them now) |
| `python tracker.py report` | Rebuilds the chart page at `docs/index.html` |
| `python tracker.py demo` | Builds `demo/demo-report.html` from simulated data, no keys needed |

## Changing what it tracks

Edit `watchlist.yaml`. Each search finds upcoming events within `look_ahead_days` and tracks all
of them. For SeatGeek, `performer` is the end of the team or artist's SeatGeek URL
(`seatgeek.com/dallas-cowboys-tickets` → `dallas-cowboys`). If a slug is wrong, the tracker
automatically falls back to a keyword search. Ticketmaster events get matched to the same
SeatGeek event (same day, same city, similar name), so each chart shows resale and face value together.

## What's in the report

- **How prices usually move in the final week:** across every event you've tracked, the lowest
  price at each point before the event as a % of its price 7 days out. This is the
  "buy now or wait?" signal, and it gets more reliable with every event you add.
- **One card per event:** lowest price now, 24-hour change, lowest price seen (and how far out),
  listing count, Ticketmaster face value, plus price and listings charts with hover details and
  a table view.
- **Filters:** All / last 7 days / last 48 hours / final 12 hours, a search box, and sorting.

## Data columns (`data/snapshots.csv`)

`snapshot_utc, source, event_id, hours_to_event, snapshot_local_weekday, snapshot_local_hour,
lowest_price, median_price, average_price, highest_price, listing_count, popularity, score, status`

Event details (venue, city, date and weekday, performers, link) are in `data/events.csv`.

## Troubleshooting

- **"SeatGeek returned no price stats"**: some SeatGeek keys come without pricing fields. Email
  SeatGeek's API support (or apply to their partner program) and ask for event `stats` access.
  Ticketmaster data keeps working in the meantime.
- **Ticketmaster shows no price**: Ticketmaster only provides price ranges for some events
  (often not for sold-out shows or before tickets go on sale).
- **GitHub runs come late**: GitHub's scheduler can run 5–20 minutes late when it's busy. That's normal.
- **Parking passes showing up**: add the word to `exclude_words` in `watchlist.yaml`.

## Ideas for next steps

- Add outside factors to each snapshot: team record or win streak, injuries, weather for outdoor games.
- Price alerts: text or email when an event drops below a target price.
- More resale sources (StubHub, Vivid Seats) once you're accepted into their affiliate/partner programs.
