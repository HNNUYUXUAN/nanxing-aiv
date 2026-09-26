"""Analytical checks for final aggregate analysis, without model/API calls."""
from itertools import product
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from scripts.build_final_analysis import (
    PIN, ROOT, worst_case, load_merge_functions, agreement_from_labels,
    association_analysis,
    distinguishable_pairs,
)
from aiv.association import lagged_process_association
from aiv.models import candidate_metric_intervals, score_outer_bounds
from aiv.metrics import NAMES, WEIGHTS


@pytest.fixture(scope='module')
def published():
    path=ROOT/'results/final-analysis/summary.json'
    if not path.exists():pytest.skip('Run build_final_analysis.py for frozen-data integration checks')
    return json.loads(path.read_text(encoding='utf-8'))


def test_missing_bounds_equal_exhaustive_small_population():
    labels=[1,4]
    result=worst_case(labels,4)
    populations=[labels+list(unknown) for unknown in product(range(1,7),repeat=2)]
    means=[np.mean(p) for p in populations]
    hot=[np.mean(np.array(p)>=4) for p in populations]
    assert result['abl_raw_lower']==min(means)
    assert result['abl_raw_upper']==max(means)
    assert result['hot_lower']==min(hot)
    assert result['hot_upper']==max(hot)


def test_no_missing_and_all_missing_have_correct_endpoints():
    known=worst_case([1,6],2)
    assert known['abl_raw_lower']==known['abl_raw_upper']==3.5
    assert known['hot_lower']==known['hot_upper']==.5
    unknown=worst_case([],3)
    assert (unknown['abl_raw_lower'],unknown['abl_raw_upper'])==(1,6)
    assert (unknown['hot_lower'],unknown['hot_upper'])==(0,1)
    assert unknown['candidate_hot'] is None
    with pytest.raises(ValueError):worst_case([1,2],1)


def test_production_merge_preserves_failed_review_fallback_and_uncertainty():
    consensus,resolve=load_merge_functions()
    judgment={'level':4,'contribution_level':None,'uncertain':True}
    done={'status':'done','judgment':judgment}
    failure={'status':'failed','judgment':None}
    assert consensus([done,done],'level',2)=={'label':None,'status':'uncertain'}
    assert consensus([done,done],'contribution_level',2)=={'label':None,'status':'abstained'}
    basic={'label':4,'status':'agreed'}
    assert resolve(basic,consensus([done,failure],'level',2),True)==dict(basic,source='fast_review_incomplete')
    conflict={'label':None,'status':'disagreement'}
    assert resolve(basic,conflict,True)==dict(conflict,source='review')
    assert resolve(basic,conflict,False)==dict(basic,source='fast')


def test_zero_perturbation_is_point_only_when_all_metrics_observed():
    metrics=candidate_metric_intervals([1,4,6],tool_count=2,verified_edges=[(0,1),(1,2)],max_changed_labels=0)
    interval=score_outer_bounds(metrics['outer_intervals'],'balanced',0)
    expected=100*sum(WEIGHTS['balanced'][i]*metrics['point_conditional_on_candidates'][name] for i,name in enumerate(NAMES))
    assert interval['lower']==pytest.approx(expected)
    assert interval['upper']==pytest.approx(expected)
    incomplete=candidate_metric_intervals([1,4,6],tool_count=2,max_changed_labels=0)
    partial=score_outer_bounds(incomplete['outer_intervals'],'balanced',0)
    assert partial['upper']-partial['lower']==pytest.approx(20)


def test_unknown_tool_is_not_verified_zero_and_label_budget_expands():
    zero=candidate_metric_intervals([1,2,4],tool_count=0)
    missing=candidate_metric_intervals([1,2,4],tool_count=None)
    assert zero['outer_intervals']['MAB']==[0,0]
    assert missing['outer_intervals']['MAB']==[0,1]
    previous=None
    for budget in range(4):
        current=candidate_metric_intervals([1,2,4],tool_count=None,max_changed_labels=budget)
        bounds=score_outer_bounds(current['outer_intervals'],'balanced',.1)
        if previous:
            assert bounds['lower']<=previous['lower']+1e-12
            assert bounds['upper']>=previous['upper']-1e-12
        previous=bounds


def test_strict_pair_separation_uses_numerical_tolerance():
    assert distinguishable_pairs([{'lower':0,'upper':20},{'lower':20,'upper':30}])==(1,0)
    assert distinguishable_pairs([{'lower':0,'upper':20},{'lower':20+1e-11,'upper':30}])==(1,0)
    assert distinguishable_pairs([{'lower':0,'upper':20},{'lower':20+1e-9,'upper':30}])==(1,1)


def test_equal_timestamps_never_create_history():
    records=[]
    for student in range(20):
        for label in (1,4):
            records.append({'student':str(student),'term':'test','timestamp':'2026-01-01','label':label})
    tied=lagged_process_association(records)
    assert tied['eligible_records']==0
    records.extend({'student':str(student),'term':'test','timestamp':'2026-01-02','label':4} for student in range(20))
    later=lagged_process_association(records)
    assert later['eligible_records']==20
    assert later['student_clusters']==20
    assert later['status']=='unavailable'


def test_source_creation_time_is_not_used_as_turn_time():
    rows=[];turns={}
    for i in range(4):
        rows.append({'record':{'id':str(i),'term':'25f','student':'synthetic-test'},
                     'dimensions':{'task':{'final':{'status':'agreed','label':4}}}})
        turns[str(i)]={'turn_timestamp':None,'record_timestamp':f'2026-01-0{i+1}'}
    result=association_analysis(SimpleNamespace(turns=turns),rows)
    assert result['eligible_records']==0
    assert result['observed_turn_timestamps']==0


def test_kappa_is_not_inflated_by_coabstention():
    result=agreement_from_labels([(1,1),(2,3),(None,None),(None,2)])
    assert result['paired_items']==4
    assert result['both_labeled']==2
    assert result['exact_including_abstention']==2
    assert result['kappa6']==pytest.approx(1/3)


def test_frozen_denominators_and_random_membership(published):
    import hashlib
    manifest=ROOT/published['source']['manifest_path']
    if not manifest.exists():pytest.skip('Private frozen sources are not shipped in aggregate-only distributions')
    assert hashlib.sha256(manifest.read_bytes()).hexdigest()==PIN
    frozen=json.loads((manifest.parent/'annotations.json').read_text(encoding='utf-8'))
    real=[r for r in frozen if r['record']['term']!='synthetic']
    assert len(real)==3515==published['denominators']['real_turns']
    assert len({(r['record']['term'],r['record']['student']) for r in real})==401
    assert sum(r['review_selection']=='random_audit' for r in real)==352
    assert sum(r['review_selection']=='high_risk' for r in real)==827
    assert sum(r['dimensions']['task']['final']['label'] is not None for r in real)==1010
    assert sum(r['dimensions']['contribution']['final']['label'] is not None for r in real)==776
    assert sum(r['dimensions']['task']['final']['label'] is not None and r['dimensions']['contribution']['final']['label'] is not None for r in real)==48


def test_human_pairing_and_timing_independent_sql(published):
    path=ROOT/'runtime/research/review.sqlite3'
    if not path.exists():pytest.skip('Private human-review database is not shipped in aggregate-only distributions')
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as connection:
        result=connection.execute("""SELECT COUNT(*),SUM(a.elapsed)/60.0,SUM(b.elapsed)/60.0
            FROM assignments a JOIN assignments b ON a.item=b.item AND a.reviewer=b.reviewer
            WHERE a.round_id='retest-v2' AND b.round_id='guided-v3'
            AND a.submitted IS NOT NULL AND b.submitted IS NOT NULL""").fetchone()
    assert result[0]==16
    assert result[1]==pytest.approx(24.55388156970342)
    assert result[2]==pytest.approx(19.414243364334105)
    assert published['human']['before_person_minutes']==pytest.approx(result[1])
    assert published['human']['after_person_minutes']==pytest.approx(result[2])


def test_flow_conserves_stratum_and_candidate_changes(published):
    for row in published['flow']:
        assert sum(row[key] for key in ['agreed','abstained','disagreement','uncertain','technical_failure'])==row['turn_denominator']
        if row['method']=='final_review_merge':
            baseline=next(r for r in published['flow'] if r['stratum']==row['stratum'] and r['dimension']==row['dimension'] and r['method']=='fast_strict')
            assert row['agreed']==baseline['agreed']+row['gained_candidate']-row['lost_candidate']
            assert row['label_or_null_changed_from_strict']==row['gained_candidate']+row['lost_candidate']+row['candidate_label_changed']


def test_sensitivity_monotonicity_pair_denominators_and_missingness(published):
    rows=published['score_range_sensitivity']
    for term in ('all','25f','26s'):
        for scheme in WEIGHTS:
            for weight in (0,.1):
                series=[r for r in rows if r['term']==term and r['scheme']==scheme and r['weight_relative_change']==weight]
                assert len(series)==4
                for a,b in zip(series,series[1:]):
                    assert b['width_median']>=a['width_median']-1e-10
                    assert b['stably_distinguishable_pairs']<=a['stably_distinguishable_pairs']
                if term=='all':
                    for row in series:
                        assert row['candidate_students']==356
                        assert row['missing_ctq_students']==356
                        assert row['missing_tool_students']==99
                        assert row['within_term_pairs']==257*256//2+99*98//2==37747
                        assert row['width_maximum']<=100


def test_public_summary_contains_no_individual_identifiers_or_text(published):
    forbidden={'student','student_id','turn_id','item','question','evidence','contribution_evidence','snapshot_json'}
    def visit(value):
        if isinstance(value,dict):
            assert not forbidden.intersection(value)
            for child in value.values():visit(child)
        elif isinstance(value,list):
            for child in value:visit(child)
    visit(published)
    assert published['association']['eligible_records']==0
    assert published['association']['student_clusters']==0
    assert published['association']['observed_turn_timestamps']==0


def test_historical_pilot_separate_denominators_and_conservation(published):
    pilot=published['pilot_exploration']
    assert pilot['original_rules_recomputed']
    groups=pilot['by_kind']
    assert {g['kind']:g['record_denominator'] for g in groups}=={'real':6,'synthetic':6}
    assert sum(g['stages']['independent']['valid'] for g in groups)==36
    assert sum(g['stages']['peer']['valid'] for g in groups)==35
    assert sum(g['stages']['arbitration']['valid'] for g in groups)==4
    assert {g['kind']:g['final_candidates'] for g in groups}=={'real':2,'synthetic':6}
    for row in pilot['comparisons']:
        assert row['candidate_count']+row['no_candidate_count']==6
        assert row['label_or_null_changes']==row['gained_candidate']+row['lost_candidate']+row['both_candidate_label_changes']
