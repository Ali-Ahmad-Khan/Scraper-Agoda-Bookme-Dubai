"""ONE-OFF, OPERATOR-APPROVED: add v2_rooms.size_sqft to production.

Scope is exactly one statement:

    ALTER TABLE v2_rooms ADD COLUMN size_sqft INT NULL, ALGORITHM=INSTANT

Nothing else. No DELETE, no UPDATE, no DROP, no data touched.

Two deliberate choices, both about not disturbing a live run:
  * appended at the END, with no `AFTER <col>` clause. Position is irrelevant
    (every statement in this codebase names its columns explicitly) and an
    `AFTER` can push MySQL off the INSTANT path into a full table rebuild.
  * `ALGORITHM=INSTANT` stated explicitly, so if this server cannot do it
    without a rebuild the statement FAILS LOUDLY instead of silently taking a
    lock on a table a publish may be writing to.

WHY: `size_sqft` is not a Bookme field. It is room size read from AGODA and
mapped into Bookme's schema, via a column this project added to UAT (approved
earlier) and never to prod. Without it, prod publishes everything except size;
`db.room_columns()` detects its absence and adapts (D-53). This migration is
the other half of that choice -- run it and size starts populating for the
94.3% of Agoda rooms that carry one.

Runs OUTSIDE db.py's additive-only guard, which refuses ALTER by design. That
is deliberate and is why this is a separate, single-purpose script rather than
a relaxation of the guard.

Idempotent: if the column already exists it reports and exits without touching
anything. Dry by default; pass --apply.
"""
import sys

from pipeline import config, db

TABLE = "v2_rooms"
COLUMN = "size_sqft"
DDL = f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} INT NULL, ALGORITHM=INSTANT"


def main(apply_it):
    config.load_env()
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT DATABASE() AS d")
        dbname = cur.fetchone()["d"]
        cur.execute(f"SHOW COLUMNS FROM {TABLE}")
        cols = [r["Field"] for r in cur.fetchall()]

    print(f"database : {dbname}")
    print(f"table    : {TABLE} ({len(cols)} columns)")
    if COLUMN in cols:
        print(f"\n{COLUMN!r} already exists -- nothing to do.")
        conn.close()
        return
    print(f"missing  : {COLUMN!r}")
    print(f"\nstatement: {DDL}")

    if not apply_it:
        print("\nDRY RUN -- nothing changed. Re-run with --apply to execute.")
        conn.close()
        return

    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {TABLE}")
        after = [r["Field"] for r in cur.fetchall()]
        cur.execute(f"SELECT COUNT(*) AS n FROM {TABLE}")
        rows = cur.fetchone()["n"]
    conn.close()

    assert COLUMN in after, f"{COLUMN} still missing after the ALTER"
    print(f"\ndone. {TABLE} now has {len(after)} columns, {rows} rows "
          f"(row count unchanged -- this adds a column, it does not touch data).")


if __name__ == "__main__":
    main("--apply" in sys.argv)
