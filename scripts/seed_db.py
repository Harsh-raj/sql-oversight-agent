"""
Builds ./data/marketplace.db from seed_schema.sql and populates it with
synthetic online course marketplace data spanning ~18 months, so
questions involving month-over-month or quarter-over-quarter comparisons
have something real to compute against.

This is an entirely invented, standalone domain (online course
marketplace) with no connection to any employer or external work data —
generated purely for demoing the agent architecture.

Run: python scripts/seed_db.py
"""

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "marketplace.db"
SCHEMA_PATH = ROOT / "data" / "seed_schema.sql"

CATEGORIES = {
    "Technology": ["Web Development", "Data Science", "Cloud Computing", "Cybersecurity", "Mobile Development"],
    "Business": ["Project Management", "Finance", "Marketing", "Entrepreneurship", "Public Speaking"],
    "Design": ["UX Design", "Graphic Design", "3D Animation", "Product Design", "Typography"],
    "Health": ["Nutrition", "Yoga", "Mental Wellness", "Fitness Coaching", "Sleep Science"],
    "Language": ["Spanish", "Mandarin", "French", "Japanese", "German"],
}
REGIONS = ["Americas", "EMEA", "APAC", "LATAM"]
STATUSES = ["published", "archived", "draft"]

random.seed(42)  # reproducible synthetic data


def random_date(start: date, end: date) -> date:
    delta_days = (end - start).days
    return start + timedelta(days=random.randint(0, delta_days))


def generate_rows(n: int, start: date, end: date):
    rows = []
    for course_id in range(1, n + 1):
        category = random.choice(list(CATEGORIES.keys()))
        subcategory = random.choice(CATEGORIES[category])
        region = random.choice(REGIONS)

        # Rough price bands per category so aggregate queries look plausible
        price_band = {
            "Technology": (30, 250),
            "Business": (20, 180),
            "Design": (25, 150),
            "Health": (10, 90),
            "Language": (15, 120),
        }[category]
        price = round(random.uniform(*price_band), 2)

        listing_date = random_date(start, end)

        # Bias toward more 'published' for older listings, more 'draft'
        # for very recent ones — mimics a real content lifecycle.
        age_days = (end - listing_date).days
        if age_days < 30:
            status = random.choices(STATUSES, weights=[0.5, 0.1, 0.4])[0]
        else:
            status = random.choices(STATUSES, weights=[0.75, 0.20, 0.05])[0]

        enrollment_count = None
        if status in ("published", "archived"):
            enrollment_count = random.randint(0, 5000)

        instructor_id = random.randint(1, 60)

        rows.append(
            (
                course_id,
                category,
                subcategory,
                region,
                price,
                listing_date.isoformat(),
                status,
                enrollment_count,
                instructor_id,
            )
        )
    return rows


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())

    end = date.today()
    start = end - timedelta(days=18 * 30)
    rows = generate_rows(n=6000, start=start, end=end)

    conn.executemany(
        """
        INSERT INTO course_listings
            (course_id, category, subcategory, platform_region, price_usd,
             listing_date, status, enrollment_count, instructor_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()

    print(f"Seeded {len(rows)} synthetic course listings into {DB_PATH}")


if __name__ == "__main__":
    main()
