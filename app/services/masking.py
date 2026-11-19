"""脱敏规则：页面标记 + 契约字段笺 → 出站规则 → 合规水线。

判定只钉一边：**提交时重算**。出站只认“脱敏刷新提交那一刻重算并落库”的规则；
敏感列在提交瞬间若仍会明文出站，整次刷新拒绝，不落半套规则。
（另一条可行路“打开瞬间冻结明文快照再下结论”本仓库不采用，也不混挂两套判定。）
"""

import asyncio
import hashlib
import json
import os

from app.config import Config
from app.db import connect, get_db
from app.services.errors import ServiceError

MASK_SENTINEL = "***"
DECISION = "提交时重算"
RESERVED_FLAGS = {"mask_digest"}
LOCK_TTL_SECONDS = 60

_LOCKS: dict[str, asyncio.Lock] = {}


def refresh_lock(db_path: str | None = None) -> asyncio.Lock:
    """同一库文件一把进程内锁，先挡住同进程并发刷新。"""
    key = os.path.abspath(db_path or Config.database())
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _digest(rows: list[list]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_field(field: str) -> bool:
    return bool(field) and len(field) <= 64 and all(
        ch.isascii() and (ch.isalnum() or ch == "_") for ch in field
    )


def rule_digest(fields: dict[str, dict]) -> str:
    return _digest(
        [[f, int(v["masked"]), v["outbound_mode"]] for f, v in sorted(fields.items())]
    )


# ---------------------------------------------------------------- 读取现状


def _contract_sensitive(conn) -> dict[str, list[str]]:
    """契约字段笺里标了敏感列的全部字段，附所在表。"""
    rows = conn.execute(
        "SELECT DISTINCT c.table_name AS table_name, f.field_name AS field_name "
        "FROM contract_fields f JOIN schema_contracts c ON c.id=f.contract_id "
        "WHERE f.sensitive=1 ORDER BY c.table_name, f.field_name"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        tables = out.setdefault(r["field_name"], [])
        if r["table_name"] not in tables:
            tables.append(r["table_name"])
    return out


def _page_flags(conn) -> dict[str, str]:
    flags: dict[str, str] = {}
    for r in conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'mask\\_%' ESCAPE '\\'"
    ).fetchall():
        field = r["key"][len("mask_"):]
        if not field or r["key"] in RESERVED_FLAGS:
            continue
        flags[field] = r["value"]
    return flags


def _page_flag(conn, field: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (f"mask_{field}",)).fetchone()
    return None if row is None else row["value"]


def _stored_rules(conn) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM mask_rules ORDER BY field").fetchall()
    return {
        r["field"]: {
            "masked": bool(r["masked"]),
            "outbound_mode": r["outbound_mode"],
            "source": r["source"],
            "sample_masked": r["sample_masked"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    }


def _stored_digest(stored: dict[str, dict]) -> str:
    return _digest([[f, int(v["masked"]), v["outbound_mode"]] for f, v in sorted(stored.items())])


def _last_success(conn):
    return conn.execute(
        "SELECT * FROM mask_refresh_runs WHERE status='成功' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def derive_rules(conn) -> dict:
    """从契约字段笺 + 页面标记推导出站规则；只推导，不写库。"""
    sensitive = _contract_sensitive(conn)
    flags = _page_flags(conn)
    stored = _stored_rules(conn)

    names = set(sensitive) | {f for f, v in flags.items() if v == "1"}
    names |= set(stored)

    fields: dict[str, dict] = {}
    fake_plaintext: list[str] = []
    page_only: list[str] = []
    auto_filled: list[str] = []
    page_closed: list[str] = []

    for field in sorted(names):
        flag = flags.get(field)
        is_sensitive = field in sensitive
        if is_sensitive:
            if flag == "0":
                # 页面脱敏关着，契约却标了敏感：提交会让明文出站，必须拒绝。
                masked = False
                fake_plaintext.append(field)
                source = "页面"
            else:
                masked = True
                if flag is None:
                    auto_filled.append(field)
                    source = "契约"
                else:
                    source = "页面+契约"
        else:
            masked = flag == "1"
            if flag == "1":
                page_only.append(field)
            elif flag == "0":
                page_closed.append(field)
            source = "页面"

        fields[field] = {
            "masked": masked,
            "outbound_mode": "masked" if masked else "plain",
            "sensitive": is_sensitive,
            "tables": sensitive.get(field, []),
            "page_flag": flag,
            "source": source,
        }

    return {
        "fields": fields,
        "fake_plaintext": fake_plaintext,
        "page_only": page_only,
        "page_closed": page_closed,
        "auto_filled": auto_filled,
        "digest": rule_digest(fields),
        "sensitive": sensitive,
    }


def _clear_plaintext_logs(conn, derived: dict, note: str) -> int:
    """敏感列的历史明文出站一律收回，改成脱敏值并打隔离标。"""
    sensitive_fields = [f for f, info in derived["fields"].items() if info["sensitive"]]
    if not sensitive_fields:
        return 0
    placeholders = ",".join("?" for _ in sensitive_fields)
    cur = conn.execute(
        f"UPDATE outbound_log SET payload=?, masked=1, quarantined=1, note=? "
        f"WHERE masked=0 AND COALESCE(quarantined,0)=0 AND field IN ({placeholders})",
        [MASK_SENTINEL, note, *sensitive_fields],
    )
    return cur.rowcount


def compliance_state(conn) -> dict:
    """合规水线：独立重算一遍，与落库规则、最近刷新摘要、出站日志逐项对表。"""
    derived = derive_rules(conn)
    stored = _stored_rules(conn)
    stored_digest = _stored_digest(stored)
    last = _last_success(conn)
    problems: list[str] = []

    if derived["fake_plaintext"]:
        names = "、".join(derived["fake_plaintext"])
        problems.append(f"{names} 在契约里标了敏感，页面脱敏却是关的，出站会落明文")
    if derived["auto_filled"]:
        names = "、".join(derived["auto_filled"])
        problems.append(f"{names} 契约已标敏感、页面还没标记，需刷新补标")
    if last is None:
        problems.append("尚未完成脱敏刷新，出站规则还没定")
    else:
        if last["digest"] != derived["digest"]:
            problems.append("页面脱敏标记改过但没刷新，出站规则与最近一次成功刷新不一致")
        if stored_digest != derived["digest"]:
            problems.append("落库出站规则与契约/页面脱敏标记对不上")

    for field, info in derived["fields"].items():
        if not info["sensitive"]:
            continue
        rule = stored.get(field)
        if not rule:
            problems.append(f"{field} 没有落库出站规则")
        elif not rule["masked"] or rule["outbound_mode"] != "masked":
            problems.append(f"{field} 的落库规则仍是明文")

    sensitive_fields = [f for f, info in derived["fields"].items() if info["sensitive"]]
    leaks = 0
    if sensitive_fields:
        placeholders = ",".join("?" for _ in sensitive_fields)
        leaks = conn.execute(
            f"SELECT COUNT(*) AS n FROM outbound_log WHERE masked=0 "
            f"AND COALESCE(quarantined,0)=0 AND field IN ({placeholders})",
            sensitive_fields,
        ).fetchone()["n"]
        if leaks:
            problems.append(f"出站日志里还有 {leaks} 条敏感列明文没收回")

    problems = list(dict.fromkeys(problems))
    ok = not problems
    masked_count = sum(1 for info in derived["fields"].values() if info["masked"])
    detail = (
        f"合规通过：{masked_count} 列按「{DECISION}」规则脱敏，出站无明文。"
        if ok
        else "合规挂红：" + "；".join(problems) + "。"
    )
    return {
        "ok": ok,
        "detail": detail,
        "problems": problems,
        "leaks": leaks,
        "digest": derived["digest"],
        "digest_short": derived["digest"][:12],
        "stored_digest": stored_digest,
        "last_refresh_at": last["created_at"] if last else "",
        "last_refresh_digest": last["digest"] if last else "",
        "decision": DECISION,
    }


def _outbound_log(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, field, payload, masked, quarantined, verified, digest, note, created_at "
        "FROM outbound_log ORDER BY id DESC LIMIT 10"
    ).fetchall()
    return [dict(r) for r in rows]


def mask_status(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        derived = derive_rules(conn)
        stored = _stored_rules(conn)
        last = _last_success(conn)
        compliance = compliance_state(conn)
        runs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM mask_refresh_runs ORDER BY id DESC LIMIT 12"
            ).fetchall()
        ]
        notes = [
            dict(r)
            for r in conn.execute(
                "SELECT c.table_name, c.version_no, c.status, f.field_name, "
                "f.field_type, f.sensitive FROM contract_fields f "
                "JOIN schema_contracts c ON c.id=f.contract_id "
                "ORDER BY c.table_name, c.version_no, f.field_name"
            ).fetchall()
        ]
        outbound_log = _outbound_log(conn)

    fields = []
    for field, info in sorted(derived["fields"].items()):
        rule = stored.get(field, {})
        rule_masked = bool(rule.get("masked"))
        outbound_mode = rule.get("outbound_mode") or info["outbound_mode"]
        fields.append(
            {
                "field": field,
                "tables": info["tables"],
                "sensitive": info["sensitive"],
                "page_masked": info["masked"],
                "page_flag": info["page_flag"],
                "source": info["source"],
                "rule_masked": rule_masked,
                "outbound_mode": outbound_mode,
                "rule_matches": rule_masked == info["masked"]
                and outbound_mode == info["outbound_mode"],
                "sample_masked": rule.get("sample_masked") or "",
                "outbound_sample": MASK_SENTINEL
                if rule_masked
                else ("（无规则）" if not rule else "明文"),
                "written_at": rule.get("updated_at", ""),
            }
        )

    return {
        "decision": DECISION,
        "chip": {
            "decision": DECISION,
            "label": (
                "未重算"
                if last is None
                else ("已重算" if last["digest"] == derived["digest"] else "已过期·待刷新")
            ),
            "digest": last["digest"] if last else "",
            "digest_short": last["digest"][:12] if last else "",
            "at": last["created_at"] if last else "",
            "run_id": last["id"] if last else None,
            "stale": bool(last is not None and last["digest"] != derived["digest"]),
            "current_digest_short": derived["digest"][:12],
        },
        "fields": fields,
        "field_notes": notes,
        "runs": runs,
        "compliance": compliance,
        "outbound_log": outbound_log,
    }


# ---------------------------------------------------------------- 刷新 / 写入


def _acquire_db_lock(conn, holder: str) -> bool:
    row = conn.execute("SELECT active, acquired_at FROM mask_lock WHERE id=1").fetchone()
    if row and row["active"]:
        fresh = conn.execute(
            "SELECT (julianday('now')-julianday(?))*86400 < ? AS fresh",
            (row["acquired_at"], LOCK_TTL_SECONDS),
        ).fetchone()["fresh"]
        if fresh:
            return False
    conn.execute(
        "UPDATE mask_lock SET active=1, holder=?, acquired_at=datetime('now') WHERE id=1",
        (holder,),
    )
    return True


def _release_db_lock(conn) -> None:
    conn.execute(
        "UPDATE mask_lock SET active=0, holder='', acquired_at=datetime('now') WHERE id=1"
    )


def _acquire_slot(db_path: str | None, holder: str) -> bool:
    """短事务抢锁：抢到立刻提交，锁行带 TTL，进程崩了也不会永久占着。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ok = _acquire_db_lock(conn, holder)
        if ok:
            conn.commit()
        else:
            conn.rollback()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_slot(db_path: str | None) -> None:
    conn = connect(db_path)
    try:
        _release_db_lock(conn)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def refresh_masking(db_path: str | None = None, holder: str = "合规员") -> dict:
    """提交时重算：页面/契约核对 → 重算出站规则 → 拒明文 → 收回历史明文 → 落库。"""
    if not _acquire_slot(db_path, holder):
        raise ServiceError("另一名合规员正在刷新脱敏规则，请稍后再试。", 409)
    try:
        return _refresh_locked(db_path)
    finally:
        _release_slot(db_path)


def _refresh_locked(db_path: str | None) -> dict:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        derived = derive_rules(conn)
        old_rules = _stored_rules(conn)

        if derived["fake_plaintext"]:
            names = "、".join(derived["fake_plaintext"])
            detail = (
                f"脱敏刷新被拒：{names} 在契约里标了敏感列，页面脱敏却是关的，"
                f"提交会让明文出站。请先在脱敏列表把 {names} 的脱敏打开，再刷新。"
            )
            conn.execute(
                "INSERT INTO mask_refresh_runs(status, decision, digest, detail, changed) "
                "VALUES ('拒绝', ?, ?, ?, 0)",
                (DECISION, derived["digest"], detail),
            )
            conn.execute("COMMIT")
            raise ServiceError(detail, 409)

        changed = 0
        conn.execute("DELETE FROM mask_rules")
        for field, info in sorted(derived["fields"].items()):
            conn.execute(
                "INSERT INTO mask_rules(field, masked, outbound_mode, source, sample_masked, updated_at) "
                "VALUES (?,?,?,?,?,datetime('now'))",
                (
                    field,
                    1 if info["masked"] else 0,
                    info["outbound_mode"],
                    info["source"],
                    MASK_SENTINEL if info["masked"] else "",
                ),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"mask_{field}", "1" if info["masked"] else "0"),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"outbound_{field}_mode", info["outbound_mode"]),
            )
            before = old_rules.get(field)
            if (
                before is None
                or before["masked"] != info["masked"]
                or before["outbound_mode"] != info["outbound_mode"]
            ):
                changed += 1

        changed += _clear_plaintext_logs(conn, derived, "脱敏刷新时收回历史明文")

        for key, value in (
            ("outbound_digest", derived["digest"]),
            ("outbound_decision", DECISION),
        ):
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('outbound_refreshed_at', datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

        notes = []
        if derived["page_only"]:
            notes.append(f"仅页面标记（契约未标敏感）：{'、'.join(derived['page_only'])}")
        if derived["auto_filled"]:
            notes.append(f"页面缺标记，已按契约补标脱敏：{'、'.join(derived['auto_filled'])}")
        sensitive_count = sum(1 for f in derived["fields"].values() if f["sensitive"])
        detail = (
            f"脱敏刷新成功：敏感列 {sensitive_count} 项，出站规则已按「{DECISION}」重算并落库，"
            f"明文出站已拒，历史明文已收回。"
            + (("；" + "；".join(notes)) if notes else "")
        )
        cur = conn.execute(
            "INSERT INTO mask_refresh_runs(status, decision, digest, detail, changed) "
            "VALUES ('成功', ?, ?, ?, ?)",
            (DECISION, derived["digest"], detail, changed),
        )
        run_id = cur.lastrowid
        row = dict(conn.execute("SELECT * FROM mask_refresh_runs WHERE id=?", (run_id,)).fetchone())
        compliance = compliance_state(conn)
        conn.execute("COMMIT")

        return {
            "ok": True,
            "decision": DECISION,
            "run": row,
            "changed": changed,
            "digest": derived["digest"],
            "digest_short": derived["digest"][:12],
            "chip": {
                "decision": DECISION,
                "label": "已重算",
                "digest": derived["digest"],
                "digest_short": derived["digest"][:12],
                "at": row["created_at"],
                "run_id": run_id,
            },
            "fields": [
                {
                    "field": f,
                    "masked": info["masked"],
                    "sensitive": info["sensitive"],
                    "outbound_mode": info["outbound_mode"],
                    "source": info["source"],
                }
                for f, info in sorted(derived["fields"].items())
            ],
            "notes": notes,
            "compliance": compliance,
            "message": detail,
        }
    except ServiceError:
        raise
    except Exception as exc:  # 失败吐白话，不把栈丢给页面
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise ServiceError(f"脱敏刷新失败：{exc}。本次没有写入任何规则。", 500)
    finally:
        conn.close()


def set_page_mask(field: str, masked_on: bool, db_path: str | None = None) -> dict:
    """页面开关只改页面标记；出站规则仍只在刷新提交时重算。"""
    field = (field or "").strip()
    if not field:
        raise ServiceError("缺少脱敏列名。", 400)
    if not _valid_field(field):
        raise ServiceError("脱敏列名只能由字母、数字、下划线组成。", 400)
    with get_db(db_path) as conn:
        sensitive = _contract_sensitive(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"mask_{field}", "1" if masked_on else "0"),
        )
        compliance = compliance_state(conn)
    if not masked_on and field in sensitive:
        message = (
            f"页面脱敏已关闭，但 {field} 在契约里是敏感列：刷新会被拒，"
            f"出站前必须打开。"
        )
    else:
        message = (
            f"页面脱敏已{'打开' if masked_on else '关闭'}；"
            f"点“刷新脱敏”提交后才重算出站规则。"
        )
    return {
        "ok": True,
        "field": field,
        "page_masked": masked_on,
        "sensitive": field in sensitive,
        "message": message,
        "compliance": compliance,
    }


# ---------------------------------------------------------------- 出站


def outbound(field: str, value: str, db_path: str | None = None) -> str:
    """清洗侧出站：敏感列以契约为底线，规则再陈旧也不许明文。"""
    conn = connect(db_path)
    try:
        info = derive_rules(conn)["fields"].get(field)
        rule = _stored_rules(conn).get(field)
    finally:
        conn.close()
    must_mask = bool(
        (info and (info["masked"] or info["sensitive"])) or (rule and rule["masked"])
    )
    if must_mask and value:
        return MASK_SENTINEL
    return value


def outbound_card(field: str, value: str, db_path: str | None = None) -> dict:
    """出站卡片：明文即拒，并把违规点讲清楚。"""
    conn = connect(db_path)
    try:
        sensitive = field in _contract_sensitive(conn)
        flag = _page_flag(conn, field)
        rule = _stored_rules(conn).get(field)
        derived = derive_rules(conn)
        last = _last_success(conn)
        compliance = compliance_state(conn)
    finally:
        conn.close()

    violations = []
    if sensitive and flag == "0":
        violations.append("页面把契约敏感列的脱敏关掉了")
    if sensitive and (not rule or not rule["masked"] or rule["outbound_mode"] != "masked"):
        violations.append("落库出站规则仍是明文")
    if sensitive and last is None:
        violations.append("尚未完成脱敏刷新")
    if sensitive and last is not None and last["digest"] != derived["digest"]:
        violations.append("出站规则与最近一次成功刷新不一致")
    if sensitive and rule and rule["masked"] and rule["outbound_mode"] != "masked":
        violations.append("落库规则自相矛盾（标记脱敏却写明文）")

    info = derived["fields"].get(field)
    must_mask = bool(
        sensitive or flag == "1" or (rule and rule["masked"]) or (info and info["masked"])
    )
    return {
        "field": field,
        "masked": must_mask,
        "outbound": MASK_SENTINEL if (must_mask and value) else value,
        "violations": violations,
        "sensitive": sensitive,
        "rule": rule,
        "compliance": compliance,
    }


def emit_outbound(field: str, value: str, db_path: str | None = None) -> dict:
    """清洗出站落库：写前自验一遍，敏感列只许脱敏值；合规侧随后还会再验。"""
    card = outbound_card(field, value, db_path)
    if card["sensitive"] and card["outbound"] != MASK_SENTINEL:
        # 敏感列只要出站值不是脱敏值，一律拒收，不写日志。
        raise ServiceError(f"拒绝出站：{field} 是敏感列，出站值会落明文。", 409)
    payload = card["outbound"]
    note = "清洗侧自验通过" if card["masked"] else "非敏感列"
    if card["violations"]:
        note += "；发现违规：" + "，".join(card["violations"])
    with get_db(db_path) as conn:
        derived = derive_rules(conn)
        cur = conn.execute(
            "INSERT INTO outbound_log(field, payload, masked, quarantined, verified, digest, note) "
            "VALUES (?,?,?,0,?,?,?)",
            (
                field,
                payload,
                1 if card["masked"] else 0,
                1 if card["masked"] else 0,
                derived["digest"],
                note,
            ),
        )
        log_id = cur.lastrowid
    return {
        "ok": True,
        "field": field,
        "outbound": payload,
        "masked": card["masked"],
        "violations": card["violations"],
        "log_id": log_id,
        "digest": derived["digest"],
        "message": "出站已脱敏落库。" if card["masked"] else "非敏感列，按规则可直接出站。",
    }


def masked(field: str, db_path: str | None = None) -> bool:
    with get_db(db_path) as conn:
        flag = _page_flag(conn, field)
        if flag is not None:
            return flag == "1"
        return field in _contract_sensitive(conn)
