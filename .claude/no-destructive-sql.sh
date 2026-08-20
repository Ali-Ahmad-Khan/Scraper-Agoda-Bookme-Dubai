#!/bin/bash
# Deterministic guard: NOTHING may delete or drop data in this project.
#
# Runs as a Claude Code PreToolUse hook on every Bash call. Exit 2 blocks the
# command and hands the message back to the model. This is not advice the model
# can reason its way past -- the command never executes.
#
# WHY: the database is PRODUCTION (bookme_sky_prod). The pipeline's own runtime
# guard (db.py::_sql) already refuses UPDATE/DELETE/ALTER/TRUNCATE, but one-off
# scripts run OUTSIDE that guard by design -- which is exactly the surface that
# could destroy live data. `cleanup_for_fresh_run.py --apply` really does delete
# v2_rooms and v2_attachments rows; it was written for UAT and would be
# catastrophic against prod.
#
# ALLOWED, deliberately: ALTER TABLE ... ADD COLUMN. Additive DDL only, and the
# operator approved exactly one such change (v2_rooms.size_sqft). Any DROP /
# MODIFY / CHANGE inside an ALTER is still blocked.

payload=$(cat)
cmd=$(printf '%s' "$payload" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print((d.get("tool_input") or {}).get("command", ""))
except Exception:
    print("")' 2>/dev/null)

[ -z "$cmd" ] && exit 0

# Normalise: strip newlines, collapse whitespace, uppercase for matching.
probe=$(printf '%s' "$cmd" | tr '\n' ' ' | tr -s ' ' | tr '[:lower:]' '[:upper:]')

block() {
    echo "BLOCKED by .claude/no-destructive-sql.sh: $1" >&2
    echo "This project is wired to PRODUCTION (bookme_sky_prod). Deleting or" >&2
    echo "dropping data is out of bounds. If this is genuinely required, the" >&2
    echo "operator must run it by hand -- it will not run from here." >&2
    exit 2
}

case "$probe" in
    *"DELETE FROM"*)            block "SQL DELETE" ;;
    *"DROP TABLE"*)             block "SQL DROP TABLE" ;;
    *"DROP DATABASE"*)          block "SQL DROP DATABASE" ;;
    *"DROP COLUMN"*)            block "SQL DROP COLUMN" ;;
    *"TRUNCATE"*)               block "SQL TRUNCATE" ;;
    *"CLEANUP_FOR_FRESH_RUN"*)  block "cleanup_for_fresh_run.py (it DELETEs rows)" ;;
esac

# ALTER is permitted ONLY as an additive ADD COLUMN.
case "$probe" in
    *"ALTER TABLE"*)
        case "$probe" in
            *"ADD COLUMN"*) : ;;
            *) block "ALTER TABLE that is not an additive ADD COLUMN" ;;
        esac
        ;;
esac

exit 0
