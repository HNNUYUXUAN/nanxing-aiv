"""Offline, evidence-bounded analysis for the frozen 384-record sample."""
from collections import Counter,defaultdict
import numpy as np
import pandas as pd
from .metrics import metrics,composite,WEIGHTS,NAMES


def kappa_from_counts(matrix):
    matrix=np.asarray(matrix,float);n=matrix.sum()
    if n==0:return None
    expected=np.dot(matrix.sum(0),matrix.sum(1))/n**2
    return float((np.trace(matrix)/n-expected)/(1-expected)) if expected<1-1e-12 else None


def paired_agreement(rows,models,draws=2000,seed=26):
    grouped=defaultdict(list);joint_null=one_null=valid=0
    for r in rows:
        es={e['model']:e['judgment'] for e in r['independent']+r['review']}
        a,b=(es.get(m) for m in models)
        if not a or not b:continue
        valid+=1;x,y=a['level'],b['level']
        if x is None and y is None:joint_null+=1
        elif x is None or y is None:one_null+=1
        else:grouped[r['record']['student']].append((x-1,y-1))
    blocks=[]
    for pairs in grouped.values():
        mat=np.zeros((6,6),int)
        for x,y in pairs:mat[x,y]+=1
        blocks.append(mat)
    matrix=np.sum(blocks,axis=0) if blocks else np.zeros((6,6),int)
    def three(m):return m.reshape(3,2,3,2).sum(axis=(1,3))
    rng=np.random.default_rng(seed);sim6=[];sim3=[]
    if blocks:
        blocks=np.asarray(blocks)
        for _ in range(draws):
            m=blocks[rng.integers(len(blocks),size=len(blocks))].sum(axis=0)
            v6=kappa_from_counts(m);v3=kappa_from_counts(three(m))
            if v6 is not None:sim6.append(v6)
            if v3 is not None:sim3.append(v3)
    return {'models':models,'dual_valid':valid,'both_abstained':joint_null,'one_abstained':one_null,
        'dual_nonnull':int(matrix.sum()),'student_clusters_dual_nonnull':len(grouped),
        'raw_agreement_including_abstention':(np.trace(matrix)+joint_null)/valid if valid else None,
        'kappa6':kappa_from_counts(matrix),'kappa3':kappa_from_counts(three(matrix)),
        'kappa6_cluster_interval95':np.quantile(sim6,[.025,.975]).tolist() if sim6 else None,
        'kappa3_cluster_interval95':np.quantile(sim3,[.025,.975]).tolist() if sim3 else None,
        'matrix6':matrix.tolist(),'matrix3':three(matrix).tolist(),
        'scope':'Conditional on both models assigning a level; whole-student bootstrap; model agreement is not accuracy.',
        'three_tiers':'L1-2 / L3-4 / L5-6; HOT remains L4-6'}


def formula_bounds(values,weights):
    """Range over unobserved submetrics in [0,1], not a confidence interval."""
    w=np.asarray(weights,float)
    low=sum(w[i]*values[name] for i,name in enumerate(NAMES) if values.get(name) is not None)
    missing=sum(w[i] for i,name in enumerate(NAMES) if values.get(name) is None)
    return [float(100*low),float(100*(low+missing))]


def student_table(rows):
    grouped=defaultdict(list)
    for r in rows:grouped[(r['record']['student'],r['record']['term'])].append(r)
    output=[]
    for (student,term),rs in sorted(grouped.items()):
        selected=sorted([r for r in rs if r['final'] is not None],key=lambda r:r['record']['timestamp'])
        agents=[r['record']['agent'] for r in selected]
        values=metrics([r['final'] for r in selected],agents,[r['record'].get('session') for r in selected])
        # These are sampled first-question records, not a complete source-turn
        # sequence. Equal session IDs cannot prove that no source turn was
        # skipped (or bridge an abstained record). No verified edge set exists.
        values['CTQ']=None
        # Partially missing tool identities do not imply complete tool breadth.
        if any(a is None for a in agents):values['MAB']=values['MAB_raw']=None
        d={'student':student,'term':term,'sampled_records':len(rs),'candidate_records':len(selected),
           'candidate_coverage':len(selected)/len(rs),'observed_agents_in_sample':len({r['record']['agent'] for r in rs if r['record']['agent']}),
           'scope':'Only sampled, model-candidate text; not student ability or learning gain.',**values}
        for name,w in WEIGHTS.items():
            d['AIV_'+name]=composite(values,w);d['rank_'+name]=None
            bounds=formula_bounds(values,w) if selected else [None,None]
            d[name+'_formula_lower'],d[name+'_formula_upper']=bounds
        d['missing_reason']='no_candidate_label' if not selected else 'session_boundary_missing'+(';tool_identity_missing' if values['MAB'] is None else '')
        output.append(d)
    return pd.DataFrame(output)


def semester_descriptions(rows,draws=2000,seed=26):
    rng=np.random.default_rng(seed);details={};samples={}
    for term in ('25f','26s'):
        subset=[r for r in rows if r['record']['term']==term];groups=defaultdict(list)
        for r in subset:groups[r['record']['student']].append(r)
        blocks=np.array([[sum(r['final'] is not None for r in rs),sum(r['final'] or 0 for r in rs),
            sum(r['final'] is not None and r['final']>=4 for r in rs)] for rs in groups.values()])
        n,total,hot=blocks.sum(axis=0)
        resampled=blocks[rng.integers(len(blocks),size=(draws,len(blocks)))].sum(axis=1)
        valid=resampled[:,0]>0;v=resampled[valid];sim=np.column_stack((v[:,1]/v[:,0],v[:,2]/v[:,0]));samples[term]=sim
        details[term]={'sampled_records':len(subset),'student_clusters':len(groups),'candidate_records':int(n),
            'ABL':float(total/n),'HOT':float(hot/n),'HOT_numerator':int(hot),
            'ABL_cluster_interval95':np.quantile(sim[:,0],[.025,.975]).tolist(),
            'HOT_cluster_interval95':np.quantile(sim[:,1],[.025,.975]).tolist(),
            'label_counts':dict(Counter(r['final'] for r in subset if r['final'] is not None))}
    diff=samples['26s']-samples['25f']
    return {'terms':details,'spring_minus_autumn':{'ABL':details['26s']['ABL']-details['25f']['ABL'],
        'HOT':details['26s']['HOT']-details['25f']['HOT'],'ABL_cluster_interval95':np.quantile(diff[:,0],[.025,.975]).tolist(),
        'HOT_cluster_interval95':np.quantile(diff[:,1],[.025,.975]).tolist()},
        'scope':'Descriptive selected-text contrast only. Fixed equal term samples and old/new mix; not population prevalence or AI causal effect.'}


def optimal_integer_allocation(sizes,sd,costs,budget,minimum=2):
    """Exact bounded allocation minimizing stratified mean variance, given known inputs."""
    sizes=np.asarray(sizes,int);sd=np.asarray(sd,float);costs=np.asarray(costs,int)
    if len(sizes)!=len(sd) or len(sd)!=len(costs) or np.any(costs<=0) or np.any(sizes<minimum):raise ValueError('Invalid strata')
    weights=sizes/sizes.sum();states={0:(0.0,[])}
    for N,s,w,c in zip(sizes,sd,weights,costs):
        new={}
        for used,(value,allocation) in states.items():
            for n in range(minimum,min(N,(budget-used)//c)+1):
                cost=used+n*c;variance=value+w*w*(1-n/N)*s*s/n
                if cost not in new or variance<new[cost][0]:new[cost]=(variance,allocation+[n])
        states=new
    if not states:return {'feasible':False}
    cost,(variance,allocation)=min(states.items(),key=lambda x:(x[1][0],x[0]))
    return {'feasible':True,'allocation':allocation,'cost':int(cost),'variance':float(variance)}
