"""One-time cleanup: reset this project's own writes so a fresh run starts clean.

Runs OUTSIDE db.py's additive-only guard, deliberately and with explicit
operator approval -- the guard exists so the PIPELINE can never delete, and
weakening it to do a one-off would remove that protection permanently.

Deletes:
  * every v2_rooms row EXCEPT the pre-project originals (KEEP_ROOM_IDS)
  * every v2_attachments row whose attachable_type is the Room model and whose
    attachable_id points at a room being deleted
  * the local ledgers, so no hotel is skipped as "already published"

Attachments are deleted FIRST and by explicit id: deleting the rooms first
would orphan them with no way left to find them (the join key is gone), and
`attachable_id` has no foreign key to follow.

Dry by default. Pass --apply to actually delete.
"""
import os
import sys

from pipeline import config, db, ledger

KEEP_ROOM_IDS = (17, 18, 36, 37, 38, 39, 43, 44, 45, 46, 47)
ROOM_TYPE = "App\\Models\\Hotels\\Room"


def main(apply_it):
    config.load_env()
    conn = db.connect()
    with conn.cursor() as cur:
        keep = ",".join(str(i) for i in KEEP_ROOM_IDS)
        cur.execute(f"SELECT id FROM v2_rooms WHERE id NOT IN ({keep})")
        room_ids = [r["id"] for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) c FROM v2_rooms")
        total = cur.fetchone()["c"]
        cur.execute("SELECT id FROM v2_rooms WHERE id IN (%s)" % keep)
        kept = [r["id"] for r in cur.fetchall()]

        att_ids = []
        if room_ids:
            # chunked: a single IN() with tens of thousands of ids overruns
            # max_allowed_packet and fails as a syntax-ish error, not an
            # obviously-too-big one
            for i in range(0, len(room_ids), 5000):
                chunk = ",".join(str(x) for x in room_ids[i:i + 5000])
                cur.execute(
                    f"SELECT id FROM v2_attachments WHERE attachable_type=%s "
                    f"AND attachable_id IN ({chunk})", (ROOM_TYPE,))
                att_ids += [r["id"] for r in cur.fetchall()]

    print(f"v2_rooms      : {total} total, keeping {len(kept)} {kept}, "
          f"DELETING {len(room_ids)}")
    print(f"v2_attachments: DELETING {len(att_ids)} (attachable_type={ROOM_TYPE})")
    for p in (ledger.PUBLISHED_PATH, ledger.UNRESOLVED_PATH):
        print(f"ledger        : {'DELETING ' if os.path.exists(p) else 'absent  '}{p}")

    if not apply_it:
        print("\nDRY RUN -- nothing deleted. Re-run with --apply to execute.")
        conn.close()
        return

    with conn.cursor() as cur:
        for i in range(0, len(att_ids), 5000):
            chunk = ",".join(str(x) for x in att_ids[i:i + 5000])
            cur.execute(f"DELETE FROM v2_attachments WHERE id IN ({chunk})")
        for i in range(0, len(room_ids), 5000):
            chunk = ",".join(str(x) for x in room_ids[i:i + 5000])
            cur.execute(f"DELETE FROM v2_rooms WHERE id IN ({chunk})")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM v2_rooms")
        left = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM v2_attachments WHERE attachable_type=%s",
                    (ROOM_TYPE,))
        att_left = cur.fetchone()["c"]
    conn.close()

    for p in (ledger.PUBLISHED_PATH, ledger.UNRESOLVED_PATH):
        if os.path.exists(p):
            os.remove(p)

    print(f"\ndone. v2_rooms now {left} (expected {len(kept)}), "
          f"room attachments now {att_left}")
    assert left == len(kept), f"expected {len(kept)} rooms to survive, found {left}"


if __name__ == "__main__":
    main("--apply" in sys.argv)
