# Dromos

**Open tools for getting your running data out of Garmin and Coros, and making sense of it.**

Dromos is a Python toolkit with a clear focus on running. It does three things:

1. **Extract**: download your activities and wellness history from Garmin (and Coros) into plain local files, untouched.
2. **Manipulate**: parse the raw files into clean, tidy tables you can query with pandas, polars or DuckDB.
3. **Visualise**: turn those tables into clear charts of training load, pace and heart rate, long-run progression and consistency.

*Dromos* (δρόμος) is Greek for a run, a race and the track it is run on.

> **Status: early development.** The package skeleton and CLI exist; extractors, parsers and charts are being built.

## Design principles

- **Raw data is write-once.** Whatever the vendor returns is stored unchanged in `data/raw/`. Everything in `data/processed/` can be rebuilt from it by re-running code.
- **Idempotent downloads.** Re-running an export skips what is already on disk, so an interrupted run can simply be restarted.
- **Your data stays yours.** `data/` and credentials are gitignored. Nothing is uploaded anywhere.
- **Be gentle with vendors.** Extraction uses the same endpoints as the vendors' web apps, not official APIs. Requests are throttled and the saved session is reused.

## Setup

Python 3.11 or 3.12.

```bash
git clone <repo-url> && cd dromos
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then fill in your Garmin login
pytest
dromos status
```

## Layout

```
src/dromos/      the package
tests/           pytest suite
data/raw/        untouched vendor output (gitignored)
data/processed/  derived tables (gitignored, rebuildable)
config.toml      paths; override with DROMOS_<KEY>
.env             credentials (gitignored), see .env.example
```

## Privacy

Activity files contain GPS tracks that usually start at your front door. Keep them out of version control, and crop or blur routes before sharing any map.

## Disclaimer

Dromos is not affiliated with Garmin or Coros. Vendor endpoints and formats change; check the current behaviour before relying on it.

## License

[MIT](LICENSE)
