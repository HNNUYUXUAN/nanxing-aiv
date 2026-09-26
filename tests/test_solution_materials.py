from itertools import product
import numpy as np
import pytest
from sklearn.metrics import cohen_kappa_score
from aiv.solution_materials import kappa_from_counts,formula_bounds,optimal_integer_allocation,student_table,paired_agreement


def test_kappa_matches_independent_library_with_fixed_categories():
    a=[1,2,2,4,4,6,1,3];b=[1,2,3,5,4,6,2,3]
    m=np.zeros((6,6),int)
    for x,y in zip(a,b):m[x-1,y-1]+=1
    assert kappa_from_counts(m)==pytest.approx(cohen_kappa_score(a,b))
    assert kappa_from_counts(np.diag([8,0,0])) is None


def test_range_uses_missingness_without_imputing_zero_score():
    values={'ABL':.5,'HOT':.25,'CTQ':None,'DHI':.75,'MAB':None}
    assert formula_bounds(values,[.2]*5)==pytest.approx([30,70])
    assert formula_bounds({k:0 for k in values},[.2]*5)==[0,0]


def test_integer_optimizer_matches_exhaustive_bounded_search():
    sizes=np.array([7,8,9]);sd=np.array([.2,.5,.3]);costs=np.array([1,2,3]);w=sizes/sizes.sum()
    got=optimal_integer_allocation(sizes,sd,costs,27)
    candidates=[(sum(w*w*(1-n/sizes)*sd*sd/n),n) for ns in product(range(2,8),range(2,9),range(2,10))
                if np.dot(n:=np.array(ns),costs)<=27]
    assert got['variance']==pytest.approx(min(v for v,_ in candidates))
    assert got['cost']<=27
    assert not optimal_integer_allocation(sizes,sd,costs,5)['feasible']


def test_unlabeled_students_and_missing_sessions_remain_in_export():
    rows=[{'record':{'student':s,'term':'26s','timestamp':'2026-01-01','agent':None,'session':None},'final':label}
          for s,label in [('a',None),('b',2),('b',4)]]
    table=student_table(rows).set_index('student')
    assert len(table)==2 and table.loc['a','candidate_records']==0
    assert table.AIV_balanced.isna().all() and table.rank_balanced.isna().all()
    assert table.CTQ.isna().all() and table.MAB.isna().all()
    assert table.loc['b','ABL_raw']==3


def test_conditional_agreement_excludes_abstention_from_kappa():
    rows=[]
    for i,(x,y) in enumerate([(1,1),(2,3),(None,None),(None,4)]):
        rows.append({'record':{'student':str(i)},'independent':[{'model':m,'judgment':{'level':v}} for m,v in [('a',x),('b',y)]],'review':[]})
    out=paired_agreement(rows,['a','b'],draws=20)
    assert out['dual_valid']==4 and out['dual_nonnull']==2
    assert out['both_abstained']==1 and out['one_abstained']==1
    assert out['raw_agreement_including_abstention']==.5


@pytest.mark.parametrize('labels', [(1,None,6), (1,6)])
def test_sampled_first_questions_never_imply_verified_source_adjacency(labels):
    rows=[{'record':{'student':'a','term':'26s','timestamp':f'2026-01-01T00:00:0{i}',
                     'agent':'fixture','session':'same-known-session'},'final':level}
          for i,level in enumerate(labels)]
    row=student_table(rows).iloc[0]
    assert row['ABL_raw']==3.5
    assert row['CTQ'] is None
    assert all(row['AIV_'+name] is None for name in ('balanced','higher_order','process'))
