"""
Cycle 48.2: Seed zip_geo with full GeoNames US ZIP dataset (~41k rows).
Idempotent — uses INSERT OR IGNORE. Safe to re-run.

Reads US.txt (GeoNames tab-separated: country, zip, city, state, state_code,
admin1, admin1_code, admin2, admin2_code, lat, lng, accuracy)
and bulk-inserts into /app/data/channelview.db::zip_geo.
"""
import sqlite3
import sys
import os

DB_PATH = os.environ.get("CV_DB_PATH", "/app/data/channelview.db")
DATA_FILE = os.environ.get("CV_ZIP_FILE", "/tmp/US.txt")

def seed(db_path, data_file):
    rows = []
    skipped = 0
    with open(data_file, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 11:
                skipped += 1
                continue
            zip_code, city, state_code = parts[1], parts[2], parts[4]
            try:
                lat = float(parts[9])
                lng = float(parts[10])
            except (ValueError, IndexError):
                skipped += 1
                continue
            if not zip_code or len(zip_code) < 3:
                skipped += 1
                continue
            rows.append((zip_code, lat, lng, city, state_code))

    print(f"Parsed {len(rows)} rows from {data_file} (skipped {skipped})")

    conn = sqlite3.connect(db_path)
    try:
        before = conn.execute("SELECT COUNT(*) FROM zip_geo").fetchone()[0]
        print(f"zip_geo before: {before} rows")

        conn.executemany(
            "INSERT OR IGNORE INTO zip_geo (zip, lat, lng, city, state) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

        after = conn.execute("SELECT COUNT(*) FROM zip_geo").fetchone()[0]
        inserted = after - before
        print(f"zip_geo after:  {after} rows (+{inserted} new)")

        # Spot checks
        samples = conn.execute(
            "SELECT zip, city, state, lat, lng FROM zip_geo WHERE zip IN ('10001','90210','33101','60601','94102','75201') ORDER BY zip"
        ).fetchall()
        print("Spot check (major metros):")
        for s in samples:
            print(f"  {s[0]} {s[1]}, {s[2]}  ({s[3]}, {s[4]})")
    finally:
        conn.close()

if __name__ == "__main__":
    seed(DB_PATH, DATA_FILE)
