-- Synthetic online course marketplace schema — a standalone, invented
-- domain unrelated to any employer or external work context. Clean,
-- well-documented column names on purpose: small local models generate
-- noticeably better SQL when the schema itself removes ambiguity.

DROP TABLE IF EXISTS course_listings;

CREATE TABLE course_listings (
    course_id         INTEGER PRIMARY KEY,
    category          TEXT NOT NULL,   -- 'Technology', 'Business', 'Design', 'Health', 'Language'
    subcategory       TEXT NOT NULL,   -- e.g. 'Web Development', 'Public Speaking', 'UX Design'
    platform_region   TEXT NOT NULL,   -- 'Americas', 'EMEA', 'APAC', 'LATAM'
    price_usd          REAL NOT NULL,
    listing_date        TEXT NOT NULL,   -- ISO date 'YYYY-MM-DD', when the course was published
    status               TEXT NOT NULL,   -- 'published', 'archived', 'draft'
    enrollment_count     INTEGER,         -- NULL if still draft
    instructor_id         INTEGER NOT NULL
);

CREATE INDEX idx_courses_category ON course_listings(category);
CREATE INDEX idx_courses_date ON course_listings(listing_date);
CREATE INDEX idx_courses_status ON course_listings(status);
