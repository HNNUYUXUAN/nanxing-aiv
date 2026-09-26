"""Separate blind and assisted human labels, immutable submissions and server timing."""

import json
from pathlib import Path
import sqlite3
import time
import uuid
import re
from .guidance import validate_guidance, PLACEHOLDER_PATTERN

class ReviewStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, question TEXT NOT NULL, ai TEXT);
            CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY,item TEXT,reviewer TEXT,mode TEXT,started REAL,submitted REAL,label INTEGER,evidence TEXT,reason TEXT,elapsed REAL,
              UNIQUE(item,reviewer,mode));
            CREATE TABLE IF NOT EXISTS reviewer_clocks(reviewer TEXT PRIMARY KEY, activated REAL NOT NULL, finished REAL);
            CREATE TABLE IF NOT EXISTS timing_resets(id TEXT PRIMARY KEY, created REAL NOT NULL, reason TEXT NOT NULL, previous_timing TEXT NOT NULL);""")
            for table in ("assignments", "reviewer_clocks"):
                if "round_id" not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN round_id TEXT NOT NULL DEFAULT 'pilot-v1'")
            columns = {r[1] for r in db.execute('PRAGMA table_info(assignments)')}
            for name, definition in {
                'guidance_json': 'TEXT', 'guidance_served_at': 'REAL',
                'fill_method': "TEXT NOT NULL DEFAULT 'manual'", 'recommendation_match': 'INTEGER',
            }.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE assignments ADD COLUMN {name} {definition}')

    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def seed(self, annotations, round_id="pilot-v1"):
        with self.db() as db:
            for a in annotations:
                r = a["record"]
                if r.get("term") == "synthetic":
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO items VALUES (?,?,?)",
                    (
                        r["id"],
                        r["question"],
                        json.dumps(a["independent"], ensure_ascii=False),
                    ),
                )
                for reviewer in ("A", "B"):
                    db.execute(
                        "INSERT OR IGNORE INTO assignments(id,item,reviewer,mode,round_id) VALUES (?,?,?,?,?)",
                        (uuid.uuid4().hex, r["id"], reviewer, "blind", round_id),
                    )

    def seed_guided(self, references, round_id='guided-v3'):
        """Append a separate assisted pass with immutable recommendation snapshots."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for reference in references:
                item = db.execute('SELECT question FROM items WHERE id=?', (reference['item'],)).fetchone()
                if item is None:
                    raise ValueError('推荐答案对应题目不存在')
                guidance = validate_guidance(reference['guidance'], item['question'])
                serialized = json.dumps(guidance, ensure_ascii=False, sort_keys=True)
                for reviewer in ('A', 'B'):
                    old = db.execute("SELECT round_id,guidance_json FROM assignments WHERE item=? AND reviewer=? AND mode='guided'", (reference['item'], reviewer)).fetchone()
                    if old:
                        if old['round_id'] != round_id or old['guidance_json'] != serialized:
                            raise ValueError('该题已有其他辅助版本，请保留既有记录并单独安排新版本')
                        continue
                    db.execute('INSERT INTO assignments(id,item,reviewer,mode,round_id,guidance_json) VALUES (?,?,?,?,?,?)',
                               (uuid.uuid4().hex, reference['item'], reviewer, 'guided', round_id, serialized))

    def progress(self):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT round_id,mode,COUNT(*) assigned,SUM(submitted IS NOT NULL) completed,COALESCE(SUM(elapsed),0) person_seconds,SUM(submitted IS NOT NULL AND elapsed IS NULL) timing_excluded FROM assignments GROUP BY round_id,mode"
                )
            ]

    def next(self, reviewer):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT a.*,i.question,i.ai FROM assignments a JOIN items i ON i.id=a.item WHERE reviewer=? AND submitted IS NULL ORDER BY a.rowid LIMIT 1",
                (reviewer,),
            ).fetchone()
            if not row:
                return None
            now = time.time()
            db.execute("""INSERT INTO reviewer_clocks(reviewer,activated,round_id) VALUES (?,?,?)
                ON CONFLICT(reviewer) DO UPDATE SET activated=excluded.activated,finished=NULL,round_id=excluded.round_id
                WHERE reviewer_clocks.round_id != excluded.round_id""", (reviewer, now, row["round_id"]))
            db.execute("UPDATE reviewer_clocks SET finished=NULL WHERE reviewer=?", (reviewer,))
            if row["started"] is None:
                db.execute(
                    "UPDATE assignments SET started=? WHERE id=?",
                    (now, row["id"]),
                )
            payload = {
                "assignment": row["id"],
                "question": row["question"],
                "mode": row["mode"],
                "round_id": row["round_id"],
                "time_limit_seconds": None,
            }
            if row['mode'] == 'guided':
                guidance = json.loads(row['guidance_json'])
                if row['guidance_served_at'] is None:
                    db.execute('UPDATE assignments SET guidance_served_at=? WHERE id=?', (now, row['id']))
                total, completed = db.execute("SELECT COUNT(*),SUM(submitted IS NOT NULL) FROM assignments WHERE reviewer=? AND round_id=? AND mode='guided'", (reviewer, row['round_id'])).fetchone()
                payload.update(guidance=guidance, position=(completed or 0)+1, total=total)
            # Never serialize ai, other labels, source identity or model names in blind mode.
            if row["mode"] not in ('blind', 'guided'):
                payload["ai"] = json.loads(row["ai"])
            return payload

    def submit(self, reviewer, assignment, label, evidence, reason, fill_method='manual'):
        if label is not None and (type(label) is not int or not 1 <= label <= 6):
            raise ValueError("标签须为1至6或不可判断")
        if (
            not isinstance(evidence, str)
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ValueError("请填写理由")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT a.*,i.question FROM assignments a JOIN items i ON i.id=a.item WHERE a.id=? AND reviewer=?",
                (assignment, reviewer),
            ).fetchone()
            if not row or row["submitted"] is not None:
                raise ValueError("任务不可提交")
            if row["started"] is None:
                raise ValueError("计时已重置，请点击“开始 / 继续”重新激活；当前填写内容可保留")
            if (label is not None and not evidence) or (
                evidence and evidence not in row["question"]
            ):
                raise ValueError("证据必须是原文连续片段")
            recommendation_match = None
            if row['mode'] == 'guided':
                if fill_method not in ('manual', 'recommendation', 'template'):
                    raise ValueError('填充方式无效')
                if row['guidance_served_at'] is None:
                    raise ValueError('请先领取辅助测评任务')
                if re.search(PLACEHOLDER_PATTERN, reason):
                    raise ValueError('请补充理由模板中的待填写内容')
                if len(reason) > 1000:
                    raise ValueError('判断理由最多 1000 字符')
                guidance = json.loads(row['guidance_json'])
                recommendation_match = int(label == guidance['label'] and evidence == guidance['evidence'] and reason == guidance['reason'])
            else:
                fill_method = 'manual'
            now = time.time()
            elapsed = max(0, now - row["started"])
            db.execute(
                "UPDATE assignments SET submitted=?,label=?,evidence=?,reason=?,elapsed=?,fill_method=?,recommendation_match=? WHERE id=?",
                (now, label, evidence, reason[:1000], elapsed, fill_method, recommendation_match, assignment),
            )
            if not db.execute("SELECT COUNT(*) FROM assignments WHERE reviewer=? AND submitted IS NULL", (reviewer,)).fetchone()[0]:
                db.execute("UPDATE reviewer_clocks SET finished=? WHERE reviewer=?", (now, reviewer))

    def timing(self, reviewer):
        with self.db() as db:
            row = db.execute("SELECT activated,finished,round_id FROM reviewer_clocks WHERE reviewer=?", (reviewer,)).fetchone()
        return {"activated_at": row["activated"] if row else None,
                "elapsed_seconds": max(0, (row["finished"] or time.time()) - row["activated"]) if row else 0,
                "running": bool(row and row["finished"] is None), "time_limit_seconds": None,
                "round_id": row["round_id"] if row else None}

    def reset_timing(self, reason):
        """Audit a timing-only reset; preserve questions and all submitted answers."""
        if not reason.strip():
            raise ValueError("Reset reason required")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = {"assignments": [dict(r) for r in db.execute("SELECT id,reviewer,mode,started,submitted,elapsed FROM assignments")],
                        "clocks": [dict(r) for r in db.execute("SELECT * FROM reviewer_clocks")]}
            reset_id = uuid.uuid4().hex
            db.execute("INSERT INTO timing_resets VALUES (?,?,?,?)", (reset_id,time.time(),reason,json.dumps(previous)))
            db.execute("UPDATE assignments SET started=NULL,elapsed=NULL")
            db.execute("DELETE FROM reviewer_clocks")
        return reset_id

    def advance(self):
        """Prepare disagreements only after every independent blind task completes."""
        with self.db() as db:
            if db.execute(
                "SELECT COUNT(*) FROM assignments WHERE mode='blind' AND submitted IS NULL"
            ).fetchone()[0]:
                return False
            rows = db.execute(
                "SELECT item,round_id,COUNT(DISTINCT COALESCE(label,0)) n FROM assignments WHERE mode='blind' GROUP BY item,round_id"
            ).fetchall()
            for row in rows:
                mode = "adjudication" if row["n"] > 1 else "assisted"
                db.execute(
                    "INSERT OR IGNORE INTO assignments(id,item,reviewer,mode,round_id) VALUES (?,?,?,?,?)",
                    (uuid.uuid4().hex, row["item"], "C", mode, row["round_id"]),
                )
        return True
