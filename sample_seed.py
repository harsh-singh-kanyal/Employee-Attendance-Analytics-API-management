"""
Seed script — loads sample data into MongoDB.

Usage:
    python sample_seed.py

Reads employees.json and attendance_logs.json from candidate_kit/sample_data/
and inserts them into the configured MongoDB database.
"""

import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

DATA_DIR = os.path.join(os.path.dirname(__file__), "candidate_kit", "sample_data")


def parse_extended_json(doc: dict) -> dict:
    """Convert MongoDB Extended JSON ($date, $oid) to Python types."""
    parsed = {}
    for key, value in doc.items():
        if isinstance(value, dict):
            if "$date" in value:
                parsed[key] = datetime.fromisoformat(
                    value["$date"].replace("Z", "+00:00")
                )
            elif "$oid" in value:
                continue  # skip _id
            else:
                parsed[key] = parse_extended_json(value)
        elif isinstance(value, list):
            parsed[key] = [
                parse_extended_json(v) if isinstance(v, dict) else v for v in value
            ]
        else:
            parsed[key] = value
    return parsed


def seed():
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]

    # --- Employees ---
    with open(os.path.join(DATA_DIR, "employees.json"), encoding="utf-8") as f:
        employees = json.load(f)

    db.employees.delete_many({})
    for emp in employees:
        db.employees.insert_one(parse_extended_json(emp))
    print(f"✓ Seeded {len(employees)} employees")

    # --- Attendance Logs ---
    with open(os.path.join(DATA_DIR, "attendance_logs.json"), encoding="utf-8") as f:
        logs = json.load(f)

    db.attendance_logs.delete_many({})
    for log in logs:
        db.attendance_logs.insert_one(parse_extended_json(log))
    print(f"✓ Seeded {len(logs)} attendance logs")

    client.close()
    print("Done.")


if __name__ == "__main__":
    seed()
