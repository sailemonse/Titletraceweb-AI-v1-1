from __future__ import annotations

import sqlite3
from pathlib import Path
from difflib import SequenceMatcher

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
REPORTS = BASE / "reports"
REPORTS.mkdir(exist_ok=True)
DB_PATH = DATA / "titletrace.db"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS properties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        address TEXT NOT NULL,
        parcel TEXT DEFAULT '',
        owner TEXT DEFAULT '',
        legal_description TEXT DEFAULT '',
        assessed_value REAL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        property_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'NOT_SEARCHED',
        source_url TEXT DEFAULT '',
        document_ref TEXT DEFAULT '',
        finding TEXT DEFAULT '',
        FOREIGN KEY(property_id) REFERENCES properties(id)
    );

    CREATE TABLE IF NOT EXISTS source_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        property_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        state TEXT NOT NULL,
        checked_at TEXT NOT NULL,
        detail TEXT DEFAULT '',
        result_json TEXT DEFAULT '',
        FOREIGN KEY(property_id) REFERENCES properties(id)
    );
    """)
    c.commit()
    c.close()


def normalize(s):
    return " ".join(str(s or "").upper().replace(",", " ").split())


def match(address, limit=10):
    query = normalize(address)
    if not query:
        return []

    c = db()
    rows = c.execute("SELECT * FROM properties ORDER BY id DESC").fetchall()
    c.close()

    results = []
    for row in rows:
        score = SequenceMatcher(None, query, normalize(row["address"])).ratio()
        if score >= 0.45:
            results.append((score, dict(row)))

    results.sort(key=lambda x: x[0], reverse=True)
    return results[:limit]


def seed_property(address):
    c = db()
    cur = c.execute(
        "INSERT INTO properties(address) VALUES (?)",
        (address.strip() or "UNRESOLVED",),
    )
    pid = cur.lastrowid
    c.commit()
    c.close()
    return pid


def report(pid):
    c = db()
    p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
    ev = c.execute(
        "SELECT * FROM evidence WHERE property_id=? ORDER BY category,id",
        (pid,),
    ).fetchall()
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"TitleTrace_property_{pid}.txt"

    lines = [
        "TITLETRACE AI",
        "Property Due-Diligence Research Report",
        "",
        f"Property: {p['address'] if p else 'UNRESOLVED'}",
        f"Parcel / RE: {p['parcel'] if p else 'UNRESOLVED'}",
        f"Owner: {p['owner'] if p else 'UNVERIFIED'}",
        f"Legal description: {p['legal_description'] if p else 'UNVERIFIED'}",
        "",
        "EVIDENCE STATUS",
    ]

    for e in ev:
        lines.append(
            f"- {e['category']}: {e['status']} | "
            f"{e['document_ref'] or 'No reference'} | "
            f"{e['finding'] or 'No finding recorded'}"
        )

    runs = c.execute(
        "SELECT source,state,checked_at,detail FROM source_runs WHERE property_id=? ORDER BY id DESC LIMIT 20",
        (pid,),
    ).fetchall() if p else []

    lines += [
        "",
        "LIVE SOURCE CHECKS",
    ]
    for r in runs:
        lines.append(f"- {r['source']}: {r['state']} | {r['checked_at']} | {r['detail']}")

    lines += [
        "",
        "IMPORTANT LIMITATION",
        "Preliminary AI-assisted property research only. "
        "Not a title commitment, title insurance policy, certified title search, "
        "survey, appraisal, or legal opinion.",
    ]

    c.close()
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def initialize_clerk_packet(pid):
    from research.clerk_packet import packet

    c = db()
    p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()

    if not p:
        c.close()
        return

    existing = {
        row["category"]
        for row in c.execute(
            "SELECT category FROM evidence WHERE property_id=?",
            (pid,),
        ).fetchall()
    }

    for source in packet(
        p["address"],
        p["parcel"],
        p["owner"],
    )["sources"]:
        if source["category"] not in existing:
            c.execute(
                """
                INSERT INTO evidence
                (property_id,category,status,source_url)
                VALUES (?,?,?,?)
                """,
                (
                    pid,
                    source["category"],
                    "NOT_SEARCHED",
                    source["url"],
                ),
            )

    c.commit()
    c.close()


def update_evidence_status(evidence_id, status, finding="", document_ref=""):
    c = db()
    c.execute(
        """
        UPDATE evidence
        SET status=?, finding=?, document_ref=?
        WHERE id=?
        """,
        (status, finding.strip(), document_ref.strip(), evidence_id),
    )
    c.commit()
    c.close()


init_db()
