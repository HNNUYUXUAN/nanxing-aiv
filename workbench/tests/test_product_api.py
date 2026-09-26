import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
from aiv.product_server import app
from aiv import product_data as data


class ProductApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'WORKBENCH_RUNTIME_ROOT':self.tmp.name,'WORKBENCH_TOKEN':'test-gate'})
        self.env.start(); self.client=TestClient(app); self.headers={'X-Workbench-Token':'test-gate'}

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def test_multiturn_chinese_import_preserves_offsets_and_missingness(self):
        text='学号,问答记录,智能体类型\nS001,"Q：解释梯度。\nA：回答\nQ：我先求偏导再检查符号，这一步正确吗？\nA：反馈",数学工具\n'
        response=self.client.post('/api/wb/import/upload',headers=self.headers,json={'name':'test.csv','text':text,'term':'25f'})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json(); self.assertEqual(result['quality']['usable'],2)
        self.assertEqual(result['quality']['raw_rows'],1); self.assertEqual(result['quality']['dropped_rows'],0)
        self.assertFalse(result['quality']['identifiable']['CTQ']['ok'])
        batch=data.get_dataset(result['id'])
        self.assertTrue(all(data.verify_record_source(r)['ok'] for r in batch['records']))
        self.assertEqual(len(data.read_json(data.runtime()/'datasets'/result['id']/'parsed-turns.json')),4)
        source=data.runtime()/batch['source_manifest']['file']
        source.write_text(text+'\n',encoding='utf-8')
        self.assertFalse(data.verify_record_source(batch['records'][0])['ok'])

    def test_jsonl_bad_row_missing_term_and_assistant_are_explicit(self):
        text='\n'.join([json.dumps({'student':'s','question':'解释梯度','role':'student'},ensure_ascii=False),'{broken',json.dumps({'student':'s','question':'辅助内容','role':'assistant'})])
        missing=data.import_text('test.jsonl',text)
        self.assertEqual(missing['quality']['usable'],0)
        result=data.import_text('test.jsonl',text,term='26s')
        self.assertEqual(result['quality']['usable'],1)
        self.assertEqual(result['quality']['raw_rows'],3)
        self.assertEqual(result['quality']['dropped_rows'],2)
        self.assertTrue(data.verify_record_source(result['records'][0])['ok'])

    def test_synthetic_flow_confirmations_and_hashed_export(self):
        get=self.client.get('/api/wb/datasets/demo-synthetic'); self.assertEqual(get.status_code,200)
        first=self.client.post('/api/wb/jobs',json={'dataset':'demo-synthetic'}).json()
        again=self.client.post('/api/wb/jobs',json={'dataset':'demo-synthetic'}).json()
        self.assertEqual(first['id'],again['id']); key=first['id']
        result=self.client.post(f'/api/wb/jobs/{key}/start'); self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['done'],192)
        rows=self.client.get(f'/api/wb/jobs/{key}/anomalies?limit=2').json()
        self.assertEqual(rows['total'],192); turn=rows['items'][0]['id']
        for decision in ('needs_review','accepted'):
            result=self.client.post(f'/api/wb/jobs/{key}/items/{turn}/confirmations',json=dict(dimension='task',decision=decision,note='合成自动测试'))
            self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(len(self.client.get(f'/api/wb/jobs/{key}/items/{turn}').json()['confirmations']),2)
        for role in ('student','teacher','administrator'):
            result=self.client.get(f'/api/wb/jobs/{key}/report?role={role}')
            self.assertEqual(result.status_code,200,result.text)
        result=self.client.get(f'/api/wb/jobs/{key}/export')
        self.assertEqual(result.status_code,200,result.text)
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            self.assertGreater(len(archive.namelist()),6)
        self.assertEqual(self.client.get('/api/wb/datasets/t3').status_code,401)

    def test_new_job_is_idempotent_without_model_calls(self):
        value=data.import_text('new.csv','student,question\ns1,请解释梯度\n',term='25f')
        first=self.client.post('/api/wb/jobs',headers=self.headers,json={'dataset':value['id']})
        self.assertEqual(first.status_code,200,first.text)
        second=self.client.post('/api/wb/jobs',headers=self.headers,json={'dataset':value['id']})
        self.assertEqual(first.json()['id'],second.json()['id'])
        self.assertEqual(first.json()['state'],'ready')
        self.assertFalse((data.runtime()/'pool.sqlite3').exists())


if __name__=='__main__': unittest.main()
