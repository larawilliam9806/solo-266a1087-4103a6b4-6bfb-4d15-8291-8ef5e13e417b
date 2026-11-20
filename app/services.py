"""业务逻辑：参考/样本上传、重验、替换、汇总查询、旧库升级。

旧库升级（数据修复）以「逐样本」为处理单位：任何时刻内存里只驻留一个样本的
原始记录，因此除单个最大样本外，内存用量不随样本总数增长。升级进度写入
app_state，按批提交；进程中断后再次启动会从上次提交的样本处继续，
重复执行结果不变（幂等）。
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter

from .db import read_tx, write_tx
from .normalization import (
    MAX_NAME_LEN,
    ValidationError,
    parse_reference,
    parse_tsv,
    validate_and_normalize,
)

# 每个写事务处理的样本数；批间提交，便于中断续跑
UPGRADE_BATCH = 50
# 页面读取时单次最多推进的批数（上传/换参考路径不设上限，保证先修复再写入）
READ_UPGRADE_BATCHES = 4
# 升级完成标记与游标
UPGRADE_DONE_KEY = "upgrade_v1_done"
UPGRADE_CURSOR_KEY = "upgrade_v1_cursor"
# 上传参考后的提示最多列出多少个失败样本（避免内存/页面随样本数增长）
FLASH_LIMIT = 20
# 校验错误中的「第 N 行」
_LINE_RE = re.compile(r"^第\s*(\d+)\s*行")


# --------------------------------------------------------------------------- #
# 参考上传
# --------------------------------------------------------------------------- #
def upload_reference(raw: bytes) -> dict:
    """上传新参考；若已存在样本，全部按新参考重新校验。

    成功后：
      * 通过重验的样本 -> VALID，变体替换为新参考下的归一化结果；
      * REF 不符 / 归一化后重复的样本 -> NEEDS_REUPLOAD，变体删除，记录原因
        与出错的原文件行号，从汇总剔除；
      * 旧参考及旧变体在同一事务中整体替换，失败则全部回滚。
    """
    run_upgrade()
    sequence = parse_reference(raw)
    with write_tx() as conn:
        cur = conn.execute(
            "INSERT INTO reference_sequences (sequence, length) VALUES (?, ?)",
            (sequence, len(sequence)),
        )
        new_ref_id = cur.lastrowid

        # 只按 id 逐个取样本，绝不 SELECT 出全部 raw_tsv；每个样本在
        # _revalidate_sample 内单独读一次原文并立即释放，内存不随样本数增长。
        revalidated = 0
        failed = 0
        failed_rows = []
        last_id = 0
        while True:
            page = conn.execute(
                "SELECT id FROM samples WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, UPGRADE_BATCH),
            ).fetchall()
            if not page:
                break
            for row in page:
                sample_id = last_id = row["id"]
                if _revalidate_sample(conn, sample_id, new_ref_id, sequence):
                    revalidated += 1
                else:
                    failed += 1
                    if len(failed_rows) < FLASH_LIMIT:
                        failed_rows.append(
                            conn.execute(
                                "SELECT name, status, error, error_line "
                                "FROM samples WHERE id=?",
                                (sample_id,),
                            ).fetchone()
                        )

        # 本事务已把所有样本落到新参考下，旧库修复标记不再需要
        _set_state(conn, UPGRADE_DONE_KEY, "1")
        _clear_state(conn, UPGRADE_CURSOR_KEY)
    return {
        "reference_id": new_ref_id,
        "length": len(sequence),
        "revalidated": revalidated,
        "failed": failed,
        "failed_rows": failed_rows,
    }


# --------------------------------------------------------------------------- #
# 样本上传
# --------------------------------------------------------------------------- #
def upload_sample(name: str, raw: bytes) -> dict:
    """上传（或同名重传）一份样本 TSV。

    重传时先完整校验新文件，成功才替换旧版本；旧变体在同一事务中删除、
    新变体写入，任何错误整体回滚，原有同名样本保持不变（包括其出错行）。
    错误信息中的行号是原文件中的真实行号。
    """
    name = (name or "").strip()
    if not name:
        raise ValidationError("样本名称不能为空。")
    if len(name) > MAX_NAME_LEN:
        raise ValidationError(f"样本名称不能超过 {MAX_NAME_LEN} 个字符。")

    run_upgrade()

    # 解析在事务外完成（不触碰数据库，不影响现有结果）。
    # parse_tsv 已成功，因此 raw 必然是合法 UTF-8。
    rows = parse_tsv(raw)
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw

    with write_tx() as conn:
        ref_row = conn.execute(
            "SELECT id, sequence FROM reference_sequences ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if ref_row is None:
            raise ValidationError("请先上传参考序列，再上传样本。")
        ref_id, ref_seq = ref_row["id"], ref_row["sequence"]

        old = conn.execute("SELECT id FROM samples WHERE name=?", (name,)).fetchone()

        # 校验 + 左对齐 + 去冗余；失败抛 ValidationError，事务回滚，旧样本保留
        variants = validate_and_normalize(rows, ref_seq)

        if old is not None:
            conn.execute("DELETE FROM sample_variants WHERE sample_id=?", (old["id"],))
            conn.execute("DELETE FROM samples WHERE id=?", (old["id"],))

        cur = conn.execute(
            "INSERT INTO samples "
            "(name, reference_id, status, error, error_line, raw_tsv) "
            "VALUES (?, ?, 'VALID', NULL, NULL, ?)",
            (name, ref_id, text),
        )
        _insert_variants(conn, cur.lastrowid, variants)

    return {"sample": name, "count": len(variants), "replaced": old is not None}


def delete_sample(sample_id: int) -> None:
    with write_tx() as conn:
        conn.execute("DELETE FROM samples WHERE id=?", (sample_id,))


# --------------------------------------------------------------------------- #
# 旧库升级（数据修复）
# --------------------------------------------------------------------------- #
def run_upgrade(max_batches: int | None = None) -> bool:
    """执行/续跑旧库修复；返回到当前时刻是否已完成（True 才开放汇总/详情）。

    幂等、可重入：并发调用由 BEGIN IMMEDIATE 串行化；每批样本提交一次，
    游标写入 app_state，中断后重新调用会从上次提交处继续。
    max_batches 限制单次调用处理的批数：页面请求用它分片推进升级，
    避免一个 HTTP 请求长时间占用；上传请求不传，确保写入前修复已全部完成。
    """
    batches = 0
    while True:
        # 先做一次廉价读检查（升级完成是绝大多数请求的路径，不应争抢写锁）
        with read_tx() as conn:
            if _read_state(conn, UPGRADE_DONE_KEY) == "1":
                return True
        if max_batches is not None and batches >= max_batches:
            return False
        with write_tx() as conn:
            if _read_state(conn, UPGRADE_DONE_KEY) == "1":
                return True
            ref = conn.execute(
                "SELECT id, sequence FROM reference_sequences "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if ref is None:
                # 还没有参考，没有可重验的样本；标记完成以免每次请求重复扫描
                _set_state(conn, UPGRADE_DONE_KEY, "1")
                _clear_state(conn, UPGRADE_CURSOR_KEY)
                return True

            cursor = int(_read_state(conn, UPGRADE_CURSOR_KEY) or 0)
            sample_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM samples WHERE id > ? ORDER BY id LIMIT ?",
                    (cursor, UPGRADE_BATCH),
                )
            ]
            for sample_id in sample_ids:
                _revalidate_sample(conn, sample_id, ref["id"], ref["sequence"])
                cursor = sample_id

            if len(sample_ids) < UPGRADE_BATCH:
                _set_state(conn, UPGRADE_DONE_KEY, "1")
                _clear_state(conn, UPGRADE_CURSOR_KEY)
                return True
            _set_state(conn, UPGRADE_CURSOR_KEY, str(cursor))
            if max_batches is not None and batches + 1 >= max_batches:
                return False
        batches += 1
        # 本批已提交；继续下一批，批间释放写锁让上传请求得以穿插


# --------------------------------------------------------------------------- #
# 单样本重验（上传参考、旧库升级共用）
# --------------------------------------------------------------------------- #
def _revalidate_sample(
    conn: sqlite3.Connection, sample_id: int, ref_id: int, ref_seq: str
) -> bool:
    """按给定参考重新解析并校验一个样本的全部原始记录，返回是否通过。

    原始 TSV（raw_tsv）保留不动，始终从库里保存的原文重验。
    通过：补齐/替换全部检出结果；失败：清空变体、标记待重传并记录原文件行号。
    """
    row = conn.execute(
        "SELECT raw_tsv, status, reference_id, error FROM samples WHERE id=?",
        (sample_id,),
    ).fetchone()
    if row is None:
        # 并发删除：该样本已不存在，视为无需处理
        return True

    try:
        rows = parse_tsv(row["raw_tsv"])
        variants = validate_and_normalize(rows, ref_seq)
    except ValidationError as exc:
        message = str(exc)
        conn.execute("DELETE FROM sample_variants WHERE sample_id=?", (sample_id,))
        conn.execute(
            "UPDATE samples SET reference_id=?, status='NEEDS_REUPLOAD', "
            "error=?, error_line=?, revalidated_at=datetime('now') WHERE id=?",
            (ref_id, message, _line_from_error(message), sample_id),
        )
        return False

    if (
        row["status"] == "VALID"
        and row["reference_id"] == ref_id
        and row["error"] is None
        and _stored_matches(conn, sample_id, variants)
    ):
        # 旧库中该样本未被旧解析器漏行：无需重写，保留原文与上传时间
        return True

    conn.execute("DELETE FROM sample_variants WHERE sample_id=?", (sample_id,))
    conn.execute(
        "UPDATE samples SET reference_id=?, status='VALID', error=NULL, "
        "error_line=NULL, revalidated_at=datetime('now') WHERE id=?",
        (ref_id, sample_id),
    )
    _insert_variants(conn, sample_id, variants)
    return True


def _stored_matches(
    conn: sqlite3.Connection, sample_id: int, variants: list[dict]
) -> bool:
    """库中现有变体是否恰好等于原文重新解析的结果（用于跳过无变化的样本）。"""
    stored = Counter(
        (r["pos"], r["ref"], r["alt"], r["gt"])
        for r in conn.execute(
            "SELECT pos, ref, alt, gt FROM sample_variants WHERE sample_id=?",
            (sample_id,),
        )
    )
    fresh = Counter((v["pos"], v["ref"], v["alt"], v["gt"]) for v in variants)
    return stored == fresh


# --------------------------------------------------------------------------- #
# 写入辅助
# --------------------------------------------------------------------------- #
def _insert_variants(
    conn: sqlite3.Connection, sample_id: int, variants: list[dict]
) -> None:
    conn.executemany(
        "INSERT INTO sample_variants "
        "(sample_id, pos, ref, alt, gt, kind, orig_pos, orig_ref, orig_alt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                sample_id,
                v["pos"],
                v["ref"],
                v["alt"],
                v["gt"],
                v["kind"],
                v["orig_pos"],
                v["orig_ref"],
                v["orig_alt"],
            )
            for v in variants
        ],
    )


def _line_from_error(message: str) -> int | None:
    match = _LINE_RE.match(message)
    return int(match.group(1)) if match else None


def _read_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None else None


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _clear_state(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM app_state WHERE key=?", (key,))


# --------------------------------------------------------------------------- #
# 读操作（同一读事务/快照内完成，页面只展示同一版参考的完整汇总）
# --------------------------------------------------------------------------- #
def get_dashboard() -> dict:
    """汇总页数据。

    旧库修复完成前不返回样本明细与汇总（页面显示升级提示），
    并在本请求内按 READ_UPGRADE_BATCHES 分片推进升级；升级完成标记与
    修复结果在同一事务提交，因此只要快照中看到标记，就一定能看到完整结果。
    """
    # 页面请求分片推进升级（每次最多 READ_UPGRADE_BATCHES 批），
    # 未完成时本请求只展示升级状态，避免长请求阻塞上传。
    run_upgrade(max_batches=READ_UPGRADE_BATCHES)
    with read_tx() as conn:
        ref = conn.execute(
            "SELECT * FROM reference_sequences ORDER BY id DESC LIMIT 1"
        ).fetchone()
        # 完成标记与最后一批样本在同一事务中提交：只要快照里看到标记，
        # 修复结果就是完整的；否则一律保持关闭，绝不展示部分修复数据。
        if _read_state(conn, UPGRADE_DONE_KEY) != "1":
            return {
                "reference": ref,
                "stats": {},
                "samples": [],
                "variants": [],
                "upgrade_pending": True,
            }

        if ref is None:
            return {
                "reference": None,
                "stats": {},
                "samples": [],
                "variants": [],
                "upgrade_pending": False,
            }

        ref_id = ref["id"]
        stats = {
            "valid": conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE reference_id=? AND status='VALID'",
                (ref_id,),
            ).fetchone()["c"],
            "needs_reupload": conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE reference_id=? "
                "AND status='NEEDS_REUPLOAD'",
                (ref_id,),
            ).fetchone()["c"],
            "stale": conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE reference_id IS NOT NULL "
                "AND reference_id<>?",
                (ref_id,),
            ).fetchone()["c"],
        }
        samples = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM sample_variants v WHERE v.sample_id=s.id) "
            "AS variant_count FROM samples s ORDER BY s.name"
        ).fetchall()
        variants = conn.execute(
            "SELECT v.pos, v.ref, v.alt, v.kind, COUNT(DISTINCT v.sample_id) AS n_samples "
            "FROM sample_variants v "
            "JOIN samples s ON s.id = v.sample_id AND s.reference_id=? AND s.status='VALID' "
            "GROUP BY v.pos, v.ref, v.alt "
            "ORDER BY v.pos, v.ref, v.alt",
            (ref_id,),
        ).fetchall()
        return {
            "reference": ref,
            "stats": stats,
            "samples": samples,
            "variants": variants,
            "upgrade_pending": False,
        }


def get_variant_detail(pos: int, ref: str, alt: str) -> dict | None:
    run_upgrade(max_batches=READ_UPGRADE_BATCHES)
    with read_tx() as conn:
        if _read_state(conn, UPGRADE_DONE_KEY) != "1":
            return {"pos": pos, "ref": ref, "alt": alt, "upgrade_pending": True}
        current = conn.execute(
            "SELECT id FROM reference_sequences ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if current is None:
            return None
        rows = conn.execute(
            "SELECT s.name AS sample, v.gt, v.orig_pos, v.orig_ref, v.orig_alt, v.kind "
            "FROM sample_variants v "
            "JOIN samples s ON s.id = v.sample_id "
            "WHERE v.pos=? AND v.ref=? AND v.alt=? "
            "AND s.reference_id=? AND s.status='VALID' "
            "ORDER BY s.name",
            (pos, ref, alt, current["id"]),
        ).fetchall()
        if not rows:
            return None
        return {
            "pos": pos,
            "ref": ref,
            "alt": alt,
            "rows": rows,
            "upgrade_pending": False,
        }
