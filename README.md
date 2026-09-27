# 🍯 Mead Batch Tracker

A lightweight web app for tracking homebrew mead batches — from planning through fermentation, aging, and tasting.

Built with Flask and SQLite. Dark-themed, mobile-friendly, zero dependencies beyond Flask.

![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/flask-3.x-black?logo=flask&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Batch management** — Create, edit, and track mead batches across their full lifecycle (planning → active → aging → bottled → drinking → archived)
- **Gravity logging** — Record hydrometer readings with auto-calculated day numbers and estimated ABV
- **Fermentation charts** — Visualize gravity and ABV over time with Chart.js
- **Nutrient schedule** — Track Fermaid O/K, DAP, GoFerm, and other additions
- **Ingredient list** — Log honey, fruit, spices, yeast, and other ingredients per batch
- **Tasting notes** — Record aroma, flavor, body, sweetness, and 1-10 ratings
- **Backup & restore** — Download your entire SQLite database as a `.db` file; restore from a previous backup with schema validation
- **Export** — JSON export (full data per batch or all batches) and CSV export (spreadsheet-friendly summary)
- **Process events** — log dilution, backsweetening, step-feeds, racking, stabilizing, spicing, and more. ABV and volume are recomputed across every volume/sugar change, so a diluted or backsweetened batch reports its real strength instead of `(OG − G) × 131.25`
- **Authentication** — HTTP Basic auth for the UI, bearer token for the API, same-origin check on form POSTs
- **Read/write REST API** — create batches, log readings, nutrients, tastings, ingredients, and process events; every write returns the batch's recomputed state
- **Dark UI** — Honey-themed dark mode with Bootstrap 5, fully responsive

## Screenshots

| Dashboard | Batch Detail |
|-----------|-------------|
| Card grid with status badges, OG, current gravity, and ABV | Full fermentation log, charts, nutrients, ingredients, tasting notes |

## Quick Start

```bash
# Clone the repo
git clone https://github.com/jcrabapple/mead-tracker.git
cd mead-tracker

# Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create credentials (writes ~/.config/mead-tracker/env, mode 600)
python scripts/set-password.py            # hidden prompt
# or: python scripts/set-password.py --random   (password saved to ~/.config/mead-tracker/credentials)

# Run the app (waitress, no debug server)
set -a; . ~/.config/mead-tracker/env; set +a
python app.py
```

Open **http://localhost:8789** in your browser. With no credentials configured every request returns 401; set `MEAD_AUTH_DISABLED=1` only for local development. `MEAD_DEV=1` runs Flask's debug server instead of waitress.

### systemd

```ini
[Service]
WorkingDirectory=/path/to/mead-tracker
EnvironmentFile=%h/.config/mead-tracker/env
ExecStart=/path/to/mead-tracker/venv/bin/python app.py
```

### Tests

```bash
pip install pytest && python -m pytest -q
```

The SQLite database (`mead.db`) is created automatically on first run.

## Project Structure

```
mead-tracker/
├── app.py              # Flask application (routes, auth, API)
├── meadcalc.py         # Event-aware ABV / volume math (pure, tested)
├── notifications.py    # Email + ntfy reminders
├── scripts/            # set-password.py, mead.py API CLI
├── tests/              # pytest suite
├── templates/
│   ├── base.html       # Dark-themed base layout
│   ├── index.html      # Dashboard with batch cards
│   ├── batch.html      # Batch detail (chart, log, nutrients, tasting)
│   └── batch_form.html # New/edit batch form
├── requirements.txt
└── .gitignore
```

## Data Model

**Batches** → track name, style, honey type, yeast, OG/FG, status, dates, notes  
**Gravity readings** → per-batch log with auto day-number from pitch date  
**Nutrient additions** → Fermaid O/K, DAP, GoFerm with amounts  
**Ingredients** → categorized (honey, fruit, spice, nutrient, yeast, other)  
**Tasting notes** → aroma, flavor, body, sweetness, 1-10 rating

## Process events and the ABV math

Each batch is replayed in date order. A **segment** starts at OG. Any event that adds liquid or sugar (dilute, backsweeten, feed, add_fruit) ends the segment:

```
alcohol carried forward = ABV_before × V_before / V_after
new segment starts at   = measured gravity after (or an estimate from volume + sugar)
ABV at gravity G        = carried + (segment_start − G) × 131.25
```

Removals (rack, volume_check) change the volume without changing concentration. `stabilize` marks the batch so "potential ABV" stops assuming the backsweetening sugar will ferment. Set **Must Volume at Pitch** on the batch when the actual liquid differs from the batch size (fruit displacement, concentrated musts), otherwise dilution ratios are off.

Event types: `dilute`, `backsweeten`, `feed`, `add_fruit`, `rack`, `volume_check`, `stabilize`, `remove_fruit`, `add_spice`, `add_oak`, `degas`, `cold_crash`, `fining`, `note`.

## API

All `/api/*` routes need `Authorization: Bearer $MEAD_API_TOKEN`. Bodies are JSON; dates are `YYYY-MM-DD` and default to today; `day_number` is computed from the pitch date if omitted. Writes return `{"created": {...}, "batch": {..., "state": {...}}}`.

| Endpoint | Method | Body / notes |
|----------|--------|-------------|
| `/api/batches` | GET | `?status=active`, `?q=name` — each batch includes `state` (current gravity, ABV, volume, potential ABV) |
| `/api/batches` | POST | `name` required; any batch field |
| `/api/batch/<id>` | GET | Full batch: readings (with ABV), events (with before/after), nutrients, ingredients, tastings, state |
| `/api/batch/<id>` | PATCH | Any batch field, e.g. `{"status": "aging", "fg": 0.998}` |
| `/api/batch/<id>?confirm=yes` | DELETE | Deletes the batch and everything under it |
| `/api/batch/<id>/readings` | GET, POST | `gravity` required; `date`, `temperature_f`, `notes` |
| `/api/batch/<id>/nutrients` | POST | `nutrient_type`, `amount` required |
| `/api/batch/<id>/tastings` | POST | `overall_rating` 1-10, `aroma`, `flavor`, `body`, `sweetness`, `notes` |
| `/api/batch/<id>/ingredients` | POST | `name` required; `category` one of honey/fruit/spice/nutrient/yeast/other |
| `/api/batch/<id>/events` | GET, POST | `event_type` required; `volume_added_gal` (or `_qt` / `_cups`), `sugar_lb` (or `sugar_oz`), `gravity_before`, `gravity_after`, `volume_after_gal`, `amount`, `notes` |
| `/api/{readings,events,nutrients,tastings,ingredients}/<row_id>` | DELETE | Remove one row |
| `/api/event-types` | GET | Event types and whether each affects the math |
| `/api/notifications/log` | GET | Recent notification sends |
| `/healthz` | GET | Unauthenticated liveness check |

`scripts/mead.py` is a small CLI over the API (`mead.py reading 3 1.050 --notes "day 5"`, `mead.py event 3 dilute --qt 1.6`). If the app sits behind Cloudflare, send a non-default User-Agent; the default `Python-urllib` UA is blocked by its bot check.

## Export Endpoints (UI auth)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/backup/download` | GET | Download full SQLite database |
| `/backup/restore` | POST | Upload and restore a `.db` backup |
| `/export/json` | GET | Export all batches (full data) as JSON |
| `/export/csv` | GET | Export batch summary as CSV |
| `/batch/<id>/export/json` | GET | Export single batch as JSON |
| `/settings` | GET | Settings page with backup/export tools |

## Mead Styles Supported

Traditional, Melomel, Metheglin, Cyser, Pyment, Braggot, Session Mead, Sack Mead, Bochet

## License

MIT
