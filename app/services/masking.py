from app.db import get_db


def masked(field: str, db_path: str | None = None) -> bool:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (f"mask_{field}",)).fetchone()
        return bool(row and row["value"] == "1")


def outbound(field: str, value: str, db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"outbound_{field}_mode",)
        ).fetchone()
        mode = row["value"] if row else "plain"
    # silent: ignore mask flag, keep plaintext
    if mode == "plain":
        return value
    return "***"
