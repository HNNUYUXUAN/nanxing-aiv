import csv
import io
from pathlib import Path
import unittest
from aiv.product_data import frozen_context, MATERIALS_PATH
from aiv.product_reports import report, results_csv, compare
from aiv.product_settings import settings


@unittest.skipUnless((Path(settings()['research_root'])/MATERIALS_PATH/'manifest.json').exists(),'Private frozen source not installed')
class FrozenProductTest(unittest.TestCase):
    def test_official_totals_and_every_student_metric(self):
        ctx=frozen_context(); result=report(ctx,'teacher')
        self.assertEqual(result['summary']['total'],3515)
        self.assertEqual(result['summary']['task_candidates'],1010)
        self.assertEqual(result['summary']['contribution_candidates'],776)
        self.assertEqual(result['summary']['student_terms'],401)
        rows=list(csv.DictReader(io.StringIO(results_csv(ctx).lstrip('\ufeff'))))
        original={(r['student'],r['term']):r for r in ctx['student_indicators']}
        self.assertEqual(len(rows),len(original))
        for row in rows:
            source=original[row['student'],row['term']]
            for metric in ('ABL','HOT','DHI','MAB'):
                a,b=row[metric],source[metric]
                if a or b: self.assertAlmostEqual(float(a),float(b),places=10)
            self.assertEqual(row['CTQ'],'');self.assertEqual(row['AIV'],'');self.assertEqual(row['rank'],'')
        independent=compare(ctx,'task','independent')
        self.assertEqual(independent['denominators']['planned_records'],3515)


if __name__=='__main__': unittest.main()
