"""回归测试：BOM / 表头 / 首行 POS=0 / 旧库升级修复 / 内存不随样本数增长。"""

import io
import os
import sqlite3
import tempfile
import unittest

from app import create_app, services
from app.db import SCHEMA
from app.normalization import (
    ValidationError,
    parse_tsv,
    validate_and_normalize,
)

REF_V1 = "GCTTTGCACGT"          # 11 bp，含 TTT 重复区
REF_V2 = "GCTATGCACGT"          # 第 4 位 T->A

BOM = "\ufeff"


def tsv(text: str) -> bytes:
    return text.encode("utf-8")


class ParseTsvHeaderTest(unittest.TestCase):
    def test_bom_without_header_keeps_first_record(self):
        rows = parse_tsv(tsv(BOM + "1\tG\tC\t0/1\n3\tT\tTT\t0/1\n"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pos"], 1)
        self.assertEqual(rows[0]["lineno"], 1)
        self.assertEqual(rows[1]["lineno"], 2)

    def test_bom_with_header(self):
        rows = parse_tsv(tsv(BOM + "POS\tREF\tALT\tGT\n1\tG\tC\t0/1\n"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lineno"], 2)

    def test_bom_with_leading_blank_lines(self):
        rows = parse_tsv(tsv(BOM + "\n\n1\tG\tC\t0/1\n"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lineno"], 3)

    def test_first_data_pos_zero_is_not_header(self):
        # 回归：首条数据 POS=0 以前会被当成表头整行跳过
        with self.assertRaises(ValidationError) as ctx:
            parse_tsv(tsv("0\tG\tC\t0/1\n"))
        self.assertIn("第 1 行", str(ctx.exception))
        self.assertIn("POS", str(ctx.exception))

    def test_pos_zero_after_header_reports_data_line(self):
        with self.assertRaises(ValidationError) as ctx:
            parse_tsv(tsv("POS\tREF\tALT\tGT\n0\tG\tC\t0/1\n"))
        self.assertIn("第 2 行", str(ctx.exception))

    def test_unknown_first_line_is_rejected_not_skipped(self):
        # 不是 POS/REF/ALT/GT 的首行一律按数据行处理并报错，绝不静默跳过
        for bad in (
            "CHROM\tPOS\tREF\tALT\tGT\n",
            "pos\tref\talt\n",
            "hello\n",
            "POS\tALT\tREF\tGT\n1\tC\tG\t0/1\n",
            "pos\tREF\tALT\tGT\n1\tG\tC\t0/1\n",
            "POS\tREF\tALT\n1\tG\tC\t0/1\n",
        ):
            with self.assertRaises(ValidationError) as ctx:
                parse_tsv(bad)
            self.assertIn("第 1 行", str(ctx.exception))

    def test_only_exact_four_column_header_is_recognized(self):
        rows = parse_tsv("POS\tREF\tALT\tGT\n1\tG\tC\t0/1\n")
        self.assertEqual(len(rows), 1)
        # 多余列的表头不是合法表头
        with self.assertRaises(ValidationError):
            parse_tsv("POS\tREF\tALT\tGT\tNOTE\n1\tG\tC\t0/1\tx\n")

    def test_bad_line_number_is_real_file_line(self):
        text = "POS\tREF\tALT\tGT\n\n\n1\tG\tC\t0/1\n\n5\tA\tC\t0/1\n"
        rows = parse_tsv(text)
        self.assertEqual(rows[1]["lineno"], 6)
        self.assertEqual(rows[1]["pos"], 5)
        with self.assertRaises(ValidationError) as ctx:
            validate_and_normalize(rows, REF_V1)
        self.assertIn("第 6 行", str(ctx.exception))

    def test_header_only_after_bom(self):
        with self.assertRaises(ValidationError):
            parse_tsv(tsv(BOM + "POS\tREF\tALT\tGT\n"))


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        os.environ["DATABASE_PATH"] = self.db_path
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.client = self.app.test_client()

    def tearDown(self):
        os.environ.pop("DATABASE_PATH", None)
        self.tmp.cleanup()

    def upload_ref(self, seq):
        payload = seq.encode() if isinstance(seq, str) else seq
        return self.client.post(
            "/reference",
            data={"reference": (io.BytesIO(payload), "ref.txt")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    def upload_sample(self, name, content):
        return self.client.post(
            "/samples",
            data={"name": name, "sample": (io.BytesIO(content), f"{name}.tsv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    def db_query(self, sql, args=()):
        with self.app.app_context():
            from app.db import get_db
            return get_db().execute(sql, args).fetchall()

    def summary(self):
        with self.app.app_context():
            dash = services.get_dashboard()
        return {
            (v["pos"], v["ref"], v["alt"]): v["n_samples"]
            for v in dash["variants"]
        }


class UploadBomTest(AppTestBase):
    def setUp(self):
        super().setUp()
        self.upload_ref(REF_V1)

    def test_bom_without_header_does_not_drop_first_record(self):
        resp = self.upload_sample("S1", tsv(BOM + "1\tG\tC\t0/1\n3\tT\tTT\t0/1\n"))
        self.assertIn("2 条变异", resp.get_data(as_text=True))
        rows = self.db_query("SELECT pos FROM sample_variants ORDER BY pos")
        self.assertEqual([r["pos"] for r in rows], [1, 2])
        self.assertEqual(self.summary()[(1, "G", "C")], 1)

    def test_bom_with_header_keeps_all_data(self):
        resp = self.upload_sample(
            "S1", tsv(BOM + "POS\tREF\tALT\tGT\n1\tG\tC\t1/1\n8\tA\tC\t0/1\n")
        )
        self.assertIn("2 条变异", resp.get_data(as_text=True))
        self.assertEqual(self.summary()[(1, "G", "C")], 1)

    def test_bom_only_file_rejected(self):
        resp = self.upload_sample("S1", tsv(BOM))
        self.assertIn("没有任何变异记录", resp.get_data(as_text=True))


class UploadErrorTest(AppTestBase):
    def setUp(self):
        super().setUp()
        self.upload_ref(REF_V1)

    def test_bad_line_reports_original_lineno_and_keeps_old_sample(self):
        self.upload_sample("S1", tsv("1\tG\tC\t0/1\n"))
        resp = self.upload_sample(
            "S1", tsv("POS\tREF\tALT\tGT\n1\tG\tC\t0/1\n\n9\tT\tTT\tBAD\n")
        )
        page = resp.get_data(as_text=True)
        self.assertIn("第 4 行", page)
        self.assertIn("GT 只允许", page)
        rows = self.db_query("SELECT v.gt, s.raw_tsv FROM samples s "
                             "JOIN sample_variants v ON v.sample_id=s.id")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gt"], "0/1")
        self.assertEqual(rows[0]["raw_tsv"], "1\tG\tC\t0/1\n")
        self.assertEqual(self.summary(), {(1, "G", "C"): 1})

    def test_bad_header_rejects_whole_file_and_keeps_old_sample(self):
        self.upload_sample("S1", tsv("1\tG\tC\t0/1\n"))
        resp = self.upload_sample("S1", tsv("CHROM\tPOS\tREF\tALT\tGT\n1\tG\tC\t0/1\n"))
        page = resp.get_data(as_text=True)
        self.assertIn("上传失败", page)
        self.assertIn("第 1 行", page)
        self.assertEqual(self.summary(), {(1, "G", "C"): 1})

    def test_failed_reupload_of_dropped_legacy_file_explains(self):
        # 旧版把 POS=0 的首行当表头丢掉，入库 1 条；新规则整份拒绝并给出原因
        self.upload_sample("S1", tsv("1\tG\tC\t0/1\n"))
        resp = self.upload_sample("S1", tsv("0\tG\tC\t0/1\n1\tG\tC\t0/1\n"))
        page = resp.get_data(as_text=True)
        self.assertIn("第 1 行", page)
        self.assertEqual(self.summary(), {(1, "G", "C"): 1})


from contextlib import contextmanager


@contextmanager
def app_services_upgrade_blocked():
    """让 run_upgrade 视为无事可做，从而保留「未完成」状态用于页面断言。"""
    original = services.run_upgrade
    services.run_upgrade = lambda max_batches=None: False
    try:
        yield
    finally:
        services.run_upgrade = original


class LegacyUpgradeTest(AppTestBase):
    """模拟修复前的旧库：样本原文被旧解析器漏掉首条记录。"""

    def _legacy_db(self, samples):
        """samples: [(name, raw_tsv, [(pos, ref, alt, gt, kind, opos, oref, oalt)])]"""
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO reference_sequences (id, sequence, length) VALUES (1, ?, ?)",
            (REF_V1, len(REF_V1)),
        )
        for name, raw, variants in samples:
            cur = conn.execute(
                "INSERT INTO samples (name, reference_id, status, error, raw_tsv) "
                "VALUES (?, 1, 'VALID', NULL, ?)",
                (name, raw),
            )
            conn.executemany(
                "INSERT INTO sample_variants "
                "(sample_id, pos, ref, alt, gt, kind, orig_pos, orig_ref, orig_alt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(cur.lastrowid, *v) for v in variants],
            )
        conn.commit()
        conn.close()

    def test_upgrade_repairs_dropped_first_record(self):
        # S1：旧版丢了 BOM 文件的首条 (8,A,C)，只剩插入 (2,C,CT)
        raw = tsv(BOM + "8\tA\tC\t1/1\n3\tT\tTT\t0/1\n")
        self._legacy_db(
            [("S1", raw, [(2, "C", "CT", "0/1", "INS", 3, "T", "TT")])]
        )
        with self.app.app_context():
            dash = services.get_dashboard()
        summary = {
            (v["pos"], v["ref"], v["alt"]): v["n_samples"] for v in dash["variants"]
        }
        self.assertEqual(summary[(2, "C", "CT")], 1)
        self.assertEqual(summary[(8, "A", "C")], 1)
        # 原文保留不变
        stored = self.db_query("SELECT raw_tsv FROM samples WHERE name='S1'")[0]
        self.assertEqual(stored["raw_tsv"], raw)
        # 原始位点保留
        row = self.db_query(
            "SELECT orig_pos FROM sample_variants WHERE sample_id=("
            "SELECT id FROM samples WHERE name='S1') AND pos=8"
        )[0]
        self.assertEqual(row["orig_pos"], 8)

    def test_upgrade_marks_bad_sample_needs_reupload_and_hides_it(self):
        # S1 正常（但库中漏了一条）；S2 原文含非法 GT 行
        raw_good = "1\tG\tC\t0/1\n3\tT\tTT\t0/1\n"
        raw_bad = "1\tG\tC\t0/1\n8\tA\tC\t0/9\n"
        self._legacy_db(
            [
                ("S1", raw_good, [(1, "G", "C", "0/1", "SNP", 1, "G", "C")]),
                ("S2", raw_bad, [(1, "G", "C", "0/1", "SNP", 1, "G", "C")]),
            ]
        )
        with self.app.app_context():
            dash = services.get_dashboard()
        rows = {r["name"]: r for r in dash["samples"]}
        self.assertEqual(rows["S1"]["status"], "VALID")
        self.assertEqual(rows["S1"]["variant_count"], 2)
        self.assertEqual(rows["S2"]["status"], "NEEDS_REUPLOAD")
        self.assertEqual(rows["S2"]["variant_count"], 0)
        self.assertEqual(rows["S2"]["error_line"], 2)
        # 汇总中只剩 S1
        for v in dash["variants"]:
            self.assertEqual(v["n_samples"], 1)
        # 页面显示原始行号
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("第 2 行", page)
        # 详情页剔除失败样本
        detail = self.client.get("/variants/1/G>C").get_data(as_text=True)
        self.assertIn("S1", detail)
        self.assertNotIn("S2", detail)

    def test_upgrade_is_gated_until_complete(self):
        raw = "1\tG\tC\t0/1\n"
        self._legacy_db([("S1", raw, [])])
        # 模拟升级尚未完成：清掉完成标记并设置游标
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('upgrade_v1_cursor', '0') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        conn.commit()
        conn.close()
        # run_upgrade 会幂等跑完，但在它完成前读取必须保持关闭
        with self.app.app_context():
            from app.db import read_tx
            with read_tx() as c:
                self.assertNotEqual(
                    services._read_state(c, services.UPGRADE_DONE_KEY), "1"
                )
            dash = services.get_dashboard()
        self.assertFalse(dash["upgrade_pending"])
        self.assertEqual(dash["stats"]["valid"], 1)

    def test_upgrade_pending_blocks_summary_and_detail_over_http(self):
        raw = "1\tG\tC\t0/1\n"
        self._legacy_db([("S1", raw, [])])
        # 强制升级保持「未完成」，模拟修复尚未完成的状态
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM app_state WHERE key='upgrade_v1_done'")
        conn.commit()
        conn.close()
        with app_services_upgrade_blocked():
            page = self.client.get("/").get_data(as_text=True)
            self.assertIn("数据升级中", page)
            self.assertNotIn("<table>", page)
            self.assertNotIn("规范变异汇总</h2>", page)
            detail = self.client.get("/variants/1/G>C").get_data(as_text=True)
            self.assertIn("数据升级中", detail)
            self.assertNotIn("<table>", detail)

    def test_interrupted_upgrade_resumes_from_cursor(self):
        samples = []
        for i in range(5):
            # 前两个样本是「中断前已处理提交」的结果，其余尚未修复
            done = [(1, "G", "C", "0/1", "SNP", 1, "G", "C")] if i < 2 else []
            samples.append((f"S{i}", "1\tG\tC\t0/1\n", done))
        self._legacy_db(samples)
        # 模拟中断：第一、二个样本已处理并提交，游标停在 id=2，完成标记不存在
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('upgrade_v1_cursor', '2') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        conn.commit()
        conn.close()
        with self.app.app_context():
            dash = services.get_dashboard()
        self.assertFalse(dash["upgrade_pending"])
        self.assertEqual(dash["stats"]["valid"], 5)
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM sample_variants")[0]["c"], 5
        )
        # 已完成标记落库，重启后不再重复修复
        state = self.db_query(
            "SELECT value FROM app_state WHERE key='upgrade_v1_done'"
        )
        self.assertEqual(state[0]["value"], "1")

    def test_upgrade_retry_is_idempotent(self):
        raw = tsv(BOM + "8\tA\tC\t1/1\n3\tT\tTT\t0/1\n")
        self._legacy_db([("S1", raw, [])])
        with self.app.app_context():
            first = services.get_dashboard()
            second = services.get_dashboard()
        self.assertEqual(
            [(v["pos"], v["ref"], v["alt"], v["n_samples"]) for v in first["variants"]],
            [(v["pos"], v["ref"], v["alt"], v["n_samples"]) for v in second["variants"]],
        )
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM sample_variants")[0]["c"], 2
        )

    def test_reference_change_repairs_too(self):
        raw = tsv(BOM + "1\tG\tC\t1/1\n8\tA\tC\t1/1\n")
        self._legacy_db([("S1", raw, [])])
        # 升级 + 换参考：S1 在新参考下 8 位为 A 仍匹配
        resp = self.upload_ref(REF_V2)
        self.assertIn("参考已更新", resp.get_data(as_text=True))
        summary = self.summary()
        self.assertEqual(summary[(1, "G", "C")], 1)
        self.assertEqual(summary[(8, "A", "C")], 1)

    def test_upgrade_memory_bounded_by_sample_not_sample_count(self):
        # 升级后 raw_tsv 一律保留；验证处理按样本逐个进行：
        # 用大量样本跑升级，确认不会一次性 SELECT 全部 raw_tsv。
        samples = []
        for i in range(120):
            raw = tsv(BOM + "1\tG\tC\t0/1\n") if i % 2 else tsv("1\tG\tC\t0/1\n")
            samples.append((f"S{i:03d}", raw, []))
        self._legacy_db(samples)
        statements = []
        with self.app.app_context():
            from app.db import get_db
            db = get_db()

            def tracer(sql):
                statements.append(sql)

            db.set_trace_callback(tracer)
            try:
                dash = services.get_dashboard()
            finally:
                db.set_trace_callback(None)
        self.assertEqual(dash["stats"]["valid"], 120)
        # 不存在一次性读取所有样本原文的查询
        for sql in statements:
            if "raw_tsv" in sql.lower() and sql.strip().upper().startswith("SELECT"):
                self.assertIn("WHERE id", sql)
                self.assertNotIn("ORDER BY id", sql)


if __name__ == "__main__":
    unittest.main()
