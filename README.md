# TitleTrace AI

Fresh deployment repository for the TitleTrace AI property due-diligence application.

## What is included

- Python web server
- Property/address intake
- SQLite property and evidence database
- Deterministic risk engine
- Evidence capture workflow
- Customer-facing TXT report
- Customer-facing PDF report
- Render deployment configuration
- `/health` endpoint
- Optional OpenAI narrative hook through `OPENAI_API_KEY`

## Render

Create a new GitHub repository and upload the contents of this folder.

Render settings:

Build Command:
`pip install -r requirements.txt`

Start Command:
`python server.py`

The application automatically uses Render's `PORT` environment variable and binds to `0.0.0.0`.

## Local Windows

Run:

`python server.py`

Then open:

`http://127.0.0.1:8000`

## Important

This application is preliminary property research software. It does not replace a title commitment, title insurance policy, certified title search, appraisal, survey, or legal opinion.


## Render / Gunicorn

This repository is configured for Render Web Service deployment with Gunicorn.

Build:
`pip install -r requirements.txt`

Start:
`python server.py`

The `wsgi.py` adapter connects Gunicorn to the TitleTrace HTTP server.


## Render deployment notes

Use the included `render.yaml` or set the start command to:
`gunicorn --bind 0.0.0.0:$PORT --timeout 600 --workers 1 wsgi:application`

The property workflow is dynamic-first: it does not download or index the entire Duval County PAO tax-roll dataset. An address is resolved against the official live parcel service and the returned parcel/RE anchors the research job.

## Data retrieval architecture

TitleTrace AI uses a dynamic-first property workflow. A submitted address is looked up against the official Duval County Property Appraiser online parcel database, and the returned parcel/RE number is used to anchor subsequent research. This build does not download or index the county's entire PAO tax-roll file.


V10 fix: the research job now trusts a verified matched_parcel in resolver diagnostics when a PAO results array is unexpectedly empty, preventing a false UNRESOLVED parcel error.
