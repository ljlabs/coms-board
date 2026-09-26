---
name: coms-board
description: Local SQLite application for shared notes, work items, questions, and a browser dashboard.
---

# Coms Board

Coms Board stores user-created records in a SQLite database and provides a command-line interface,
an HTTP API, and a browser dashboard.

## Run

```sh
python3 coms.py --help
bash scripts/serve.sh
```

The dashboard binds to `127.0.0.1` by default. It has no authentication; keep it on a trusted local
machine and do not expose it to a network.

## Data

The database defaults to `data/coms.db`. Set `COMS_DB` to use another path. Database files and
their SQLite journal files are local application data and must not be committed.

## Development

Run the Python test suite with:

```sh
python3 -m unittest discover -s tests
```
