import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('现行','草稿','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS contract_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  field_name TEXT NOT NULL,
  field_type TEXT NOT NULL,
  sensitive INTEGER NOT NULL DEFAULT 0,
  UNIQUE(contract_id, field_name)
);

CREATE TABLE IF NOT EXISTS catalog_tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  live_version INTEGER NOT NULL,
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apply_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT NOT NULL,
  matched INTEGER NOT NULL,
  drifted INTEGER NOT NULL,
  summary TEXT NOT NULL,
  compliance_ok INTEGER NOT NULL DEFAULT 0,
  compliance_detail TEXT NOT NULL DEFAULT '',
  compliance_digest TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mask_rules (
  field TEXT PRIMARY KEY,
  masked INTEGER NOT NULL DEFAULT 0,
  outbound_mode TEXT NOT NULL DEFAULT 'plain',
  source TEXT NOT NULL DEFAULT '页面',
  sample_masked TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mask_refresh_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  decision TEXT NOT NULL DEFAULT '提交时重算',
  digest TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  changed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mask_lock (
  id INTEGER PRIMARY KEY CHECK (id=1),
  active INTEGER NOT NULL DEFAULT 0,
  holder TEXT NOT NULL DEFAULT '',
  acquired_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS outbound_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  field TEXT NOT NULL,
  payload TEXT NOT NULL,
  masked INTEGER NOT NULL DEFAULT 0,
  quarantined INTEGER NOT NULL DEFAULT 0,
  verified INTEGER NOT NULL DEFAULT 0,
  digest TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED = [
    (
        "dw_orders_d",
        1,
        "现行",
        "初始现行",
        [("order_id", "INT"), ("amount", "DECIMAL"), ("biz_date", "DATE"), ("phone", "STRING")],
    ),
    (
        "dw_orders_d",
        2,
        "草稿",
        "拟删 amount 改币种",
        [("order_id", "INT"), ("currency", "STRING"), ("biz_date", "DATE"), ("phone", "STRING")],
    ),
    (
        "dw_payments_d",
        1,
        "现行",
        "支付现行",
        [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP")],
    ),
]

# 契约里已标敏感列：页面脱敏标记必须与之一致。
SENSITIVE_FIELDS = {("dw_orders_d", "phone")}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列：不重建表，不动既有数据。"""
    _ensure_column(conn, "contract_fields", "sensitive", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "reconcile_reports", "compliance_ok", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "reconcile_reports", "compliance_detail", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "reconcile_reports", "compliance_digest", "TEXT NOT NULL DEFAULT ''")


def _backfill_masking(conn: sqlite3.Connection) -> None:
    """把脱敏标记和契约敏感列补齐；页面标记与契约标记必须落在同一批列上。"""
    conn.execute("INSERT OR IGNORE INTO mask_lock(id, active) VALUES (1, 0)")
    # 老库缺 phone 列时补进 dw_orders_d 的各版本，避免页面脱敏失去契约依据。
    contracts = conn.execute(
        "SELECT id, table_name FROM schema_contracts WHERE table_name='dw_orders_d'"
    ).fetchall()
    for c in contracts:
        conn.execute(
            "INSERT OR IGNORE INTO contract_fields(contract_id, field_name, field_type, sensitive) "
            "VALUES (?,?,?,1)",
            (c["id"], "phone", "STRING"),
        )
    conn.execute(
        "UPDATE contract_fields SET sensitive=1 WHERE field_name='phone' AND contract_id IN "
        "(SELECT id FROM schema_contracts WHERE table_name='dw_orders_d')"
    )
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('mask_phone','1')")
    # 早期的明文出站点先留着：这是“敏感列已打标、出站仍明文”的现场，
    # 合规水线据此挂红，脱敏刷新提交时才收回。
    if not conn.execute("SELECT 1 FROM outbound_log WHERE field='phone'").fetchone():
        conn.execute(
            "INSERT INTO outbound_log(field, payload, masked, quarantined, verified, note) "
            "VALUES ('phone','13800138000',0,0,0,'脱敏上线前的历史明文出站')"
        )
    # 故意保留陈旧的明文出站规则：刷新提交时才重算成 masked。
    if not conn.execute("SELECT 1 FROM mask_rules WHERE field='phone'").fetchone():
        conn.execute(
            "INSERT INTO mask_rules(field, masked, outbound_mode, source, sample_masked) "
            "VALUES ('phone',1,'plain','页面+契约','')"
        )
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('outbound_phone_mode','plain')")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        if not conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            for table, ver, status, note, fields in SEED:
                cur = conn.execute(
                    "INSERT INTO schema_contracts(table_name, version_no, status, note) VALUES (?,?,?,?)",
                    (table, ver, status, note),
                )
                cid = cur.lastrowid
                for fname, ftype in fields:
                    sensitive = 1 if (table, fname) in SENSITIVE_FIELDS else 0
                    conn.execute(
                        "INSERT INTO contract_fields(contract_id, field_name, field_type, sensitive) "
                        "VALUES (?,?,?,?)",
                        (cid, fname, ftype, sensitive),
                    )
                if status == "现行":
                    conn.execute(
                        "INSERT OR REPLACE INTO catalog_tables(table_name, live_version) VALUES (?,?)",
                        (table, ver),
                    )
            conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        _backfill_masking(conn)
