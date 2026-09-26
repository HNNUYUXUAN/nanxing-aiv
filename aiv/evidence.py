"""Read-only, allowlisted provenance lookup for the private text subset."""
import csv
import hashlib
import json
from pathlib import Path
from threading import RLock

from .data import question_only


class EvidenceIndex:
    def __init__(self, root):
        self.root = Path(root)
        self._stamp = None
        self._records = []
        self._sources = {}
        self._lock = RLock()

    def records(self):
        path = self.root / 'runtime/research/records.json'
        with self._lock:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self._stamp:
                self._records = json.loads(path.read_text('utf-8'))
                self._stamp = stamp
            return self._records

    def search(self, query='', term='', offset=0, limit=25):
        words = query.casefold().split()
        rows = self.records()
        matches = []
        for r in rows:
            if term and r['term'] != term:
                continue
            text = ' '.join(str(r.get(k, '')) for k in
                            ('id', 'question', 'source_file', 'source_row', 'timestamp', 'term')).casefold()
            if all(word in text for word in words):
                matches.append(r)
        return {
            'total': len(matches), 'indexed': len(rows), 'offset': offset,
            'terms': {t: sum(r['term'] == t for r in rows) for t in ('25f', '26s')},
            'items': [{**{k: r[k] for k in ('id', 'term', 'timestamp', 'source_row')},
                       'source_file': r['source_file'].replace('\\', '/'),
                       'preview': r['question'][:180]} for r in matches[offset:offset + limit]],
        }

    def detail(self, record_id):
        record = next((r for r in self.records() if r['id'] == record_id), None)
        if record is None:
            raise KeyError(record_id)
        relative = record['source_file'].replace('\\', '/')
        if relative not in ('data/25f/csv/qa.csv', 'data/26s/csv/qa.csv'):
            raise ValueError('来源路径不在允许的问答文件中')
        path = (self.root / relative).resolve()
        if not path.is_relative_to((self.root / 'data').resolve()):
            raise ValueError('来源路径无效')
        with self._lock:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            cached = self._sources.get(relative)
            if cached is None or cached[0] != stamp:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                with path.open(encoding='utf-8-sig', newline='') as stream:
                    questions = [r.get('问答记录', '') for r in csv.DictReader(stream)]
                cached = (stamp, digest, questions)
                self._sources[relative] = cached
            if cached[1] != record['source_sha256']:
                raise ValueError('源文件已变化，索引哈希不匹配；请重新核验索引')
            row = record['source_row']
            if type(row) is not int or not 2 <= row <= len(cached[2]) + 1:
                raise ValueError('来源行号无效')
            raw_qa = cached[2][row - 2]
            if question_only(raw_qa) != record['question']:
                raise ValueError('来源行的学生提问与索引不一致')
        return {
            **{k: record[k] for k in ('id', 'term', 'timestamp', 'question', 'source_row', 'source_sha256')},
            'source_file': relative, 'raw_qa': raw_qa, 'source_verified': True,
            'row_definition': 'CSV 逻辑行号（含表头）；多行单元格不按文本编辑器物理行计数',
        }
