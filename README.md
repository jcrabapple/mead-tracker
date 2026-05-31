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
- **REST API** — JSON endpoints for future mobile app integration
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

# Run the app
python app.py
```

Open **http://localhost:8789** in your browser.

The SQLite database (`mead.db`) is created automatically on first run.

## Project Structure

```
mead-tracker/
├── app.py              # Flask application (routes, models, logic)
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

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/batches` | GET | All batches as JSON |
| `/api/batch/<id>/readings` | GET | Gravity readings for a batch |

## Mead Styles Supported

Traditional, Melomel, Metheglin, Cyser, Pyment, Braggot, Session Mead, Sack Mead, Bochet

## License

MIT
