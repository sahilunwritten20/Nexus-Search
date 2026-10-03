"""SQLite backup for Nexus Search — the online-backup-API snapshot.

Safe under WAL: sqlite3's backup() copies a CONSISTENT view (it takes the
necessary locks internally), so a live uvicorn process can keep serving
while a snapshot runs. Backs up the main DB and (optionally, --with-
frontier) the frontier DB the crawler CLI convention derives from it.

Usage:
    python -m nexus_search.core.backup --db nexus_search.db --out backups/
Restore: stop the writer, copy the file back over NEXUS_DB, start.

Retention guidance: keep N daily + M weekly snapshots; SQLite snapshots
compress ~10:1 (gzip) — this tool writes plain .db copies; compress them
in your retention script if size matters.
"""
import argparse
import os
import sqlite3
import time


def backup_db(source_path: str, out_dir: str, label: str = "nexus_search") -> str:
    """One consistent snapshot of `source_path` into `out_dir`.
    Returns the backup file path. Raises on failure — a backup that
    silently didn't happen is worse than a loud error."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_path = os.path.join(out_dir, f"{label}-{stamp}.db")
    src = sqlite3.connect(source_path)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)   # online backup API: consistent view under WAL
    finally:
        dst.close()
        src.close()
    return dest_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Consistent SQLite backup")
    parser.add_argument("--db", default=os.environ.get("NEXUS_DB", "nexus_search.db"))
    parser.add_argument("--out", default="backups")
    parser.add_argument("--with-frontier", action="store_true",
                        help="also back up the crawler frontier DB derived "
                             "from --db (same convention as the crawler CLI)")
    args = parser.parse_args(argv)
    if not os.path.exists(args.db):
        print(f"no such database: {args.db}")
        return 1
    path = backup_db(args.db, args.out, label=os.path.splitext(os.path.basename(args.db))[0])
    size = os.path.getsize(path)
    print(f"backup: {path} ({size:,} bytes)")
    if args.with_frontier:
        frontier = os.path.splitext(args.db)[0] + "_frontier.db"
        if os.path.exists(frontier):
            fpath = backup_db(frontier, args.out,
                              label=os.path.splitext(os.path.basename(frontier))[0])
            print(f"backup: {fpath} ({os.path.getsize(fpath):,} bytes)")
        else:
            print(f"no frontier DB at {frontier} (skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
