"""Copy the existing board into its private Docker volume on first start."""
import sqlite3
from pathlib import Path

from coms import db

target = db.db_path()
target.parent.mkdir(parents=True, exist_ok=True)
seed = Path('/seed/coms.db')
if not target.exists() and seed.is_file():
    source = sqlite3.connect(seed.resolve().as_uri() + '?mode=ro', uri=True)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    print('Copied the existing SQLite board into the private Docker volume.')
elif target.exists():
    print('The private Docker board already exists; keeping its data.')
else:
    print('No existing database found; creating a new board in the Docker volume.')

conn = db.connect()
conn.execute("INSERT OR REPLACE INTO runner_settings(key,value) VALUES('opencode_api_key','')")
conn.commit()
conn.close()
