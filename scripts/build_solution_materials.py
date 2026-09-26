"""Recompute private A-E analysis tables and export slide-ready figures, without APIs."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from aiv.solution_materials import paired_agreement,student_table,semester_descriptions,optimal_integer_allocation
from aiv.analysis import student_scores,weight_sensitivity,causal_simulation,redteam
from aiv.association import lagged_process_association
from aiv.calibration import residual_mean
from aiv.data import synthetic_records
from aiv.metrics import WEIGHTS,NAMES,metrics,composite

DEFAULT='runtime/research/fast-strong-384-v3-20260926T121958Z/r3/plan.json'


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def cost_experiment():
    sizes=np.array([240,120,40]);costs=np.array([1,2,3]);rates=np.array([.05,.35,.70]);rng=np.random.default_rng(26)
    strata=np.repeat(np.arange(3),sizes);truth=rng.binomial(1,rates[strata]);pred=np.array([.20,.40,.60])[strata]
    sd=np.array([truth[strata==h].std(ddof=1) for h in range(3)])
    optimum=optimal_integer_allocation(sizes,sd,costs,150)
    allocations={'equal_count':[25,25,25],'proportional':[60,30,10],'variance_cost_optimal':optimum['allocation']}
    trials=[]
    for name,allocation in allocations.items():
        for rep in range(500):
            ids=np.concatenate([rng.choice(np.flatnonzero(strata==h),n,replace=False) for h,n in enumerate(allocation)])
            result=residual_mean(pred,ids,truth[ids],strata);low,high=result['interval']
            trials.append({'strategy':name,'replicate':rep,'estimate':result['estimate'],'error':result['estimate']-truth.mean(),
                          'covered':low<=truth.mean()<=high,'width':high-low})
    table=pd.DataFrame(trials);summary=[]
    for name,allocation in allocations.items():
        t=table[table.strategy==name];coverage=float(t.covered.mean())
        summary.append({'strategy':name,'allocation':allocation,'cost_units':int(np.dot(allocation,costs)),
            'bias':float(t.error.mean()),'rmse':float(np.sqrt((t.error**2).mean())),
            'coverage':coverage,'coverage_mc_se':float(np.sqrt(coverage*(1-coverage)/len(t))),'mean_width':float(t.width.mean())})
    return {'source':'constructed_finite_population_known_truth','population':400,'budget_units':150,
        'sizes':sizes.tolist(),'assumed_label_rates':rates.tolist(),'assumed_costs':costs.tolist(),'replicates_per_strategy':500,
        'truth':float(truth.mean()),'strategies':summary,'optimal_design':optimum,
        'scope':'Oracle residual variance under independent within-stratum sampling; hypothetical cost units, not observed human minutes. Does not prove real kappa or calibration.'},table


def figures(out,rows,agreement,terms,cost,weight_info,red):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    font=Path('C:/Windows/Fonts/msyh.ttc')
    if font.exists():font_manager.fontManager.addfont(str(font));family=font_manager.FontProperties(fname=str(font)).get_name()
    else:family='DejaVu Sans'
    plt.rcParams.update({'font.family':family,'font.size':12,'axes.spines.top':False,'axes.spines.right':False,
        'axes.titleweight':'bold','axes.titlesize':18,'axes.unicode_minus':False,'svg.fonttype':'path','pdf.fonttype':42})
    folder=out/'figures';folder.mkdir(exist_ok=True);catalog=[]
    def save(fig,name,title,source,note):
        fig.suptitle(title,x=.06,ha='left',fontsize=19,fontweight='bold')
        fig.text(.06,.02,note,fontsize=10,color='#535c69')
        fig.subplots_adjust(left=.12,right=.95,top=.78,bottom=.22)
        for ext in ('png','pdf','svg'):fig.savefig(folder/(name+'.'+ext),dpi=180,facecolor='white')
        plt.close(fig);catalog.append({'id':name,'title':title,'source':source,'note':note,'formats':['png','pdf','svg']})
    colors=['#2864A5','#A7B0BD','#D4A12A','#8A6798','#A65042']
    fig,ax=plt.subplots(figsize=(12,6));left=np.zeros(2)
    for state,label,color in zip(['agreed','abstained','disagreement','uncertain','technical_failure'],['候选标签','一致弃权','分歧','不确定','技术失败'],colors):
        counts=[sum(r['record']['partition']==p and r['dimensions']['task']['final']['status']==state for r in rows) for p in ('regression','new_student')]
        widths=np.array(counts)/np.array([96,288])*100
        ax.barh([1,0],widths,left=left,label=label,color=color)
        for y,w,l,n in zip([1,0],widths,left,counts):
            if w>7:ax.text(l+w/2,y,f'{n} 条',ha='center',va='center',color='white' if state=='agreed' else '#192733')
        left+=widths
    ax.set(yticks=[1,0],yticklabels=['回归 n=96','新增 n=288'],xlim=(0,100),xlabel='占该组记录（%）')
    ax.legend(ncols=5,loc='upper center',bbox_to_anchor=(.5,1.25),frameon=False,fontsize=10)
    save(fig,'01-real-outcomes','384 条真实题的最终任务状态','batch annotations.json','来源：冻结 r3 批次。187 条候选、167 条弃权、30 条待核查；候选标签不等于正确答案。')
    fig,ax=plt.subplots(figsize=(12,6));xs=np.arange(2)
    for shift,key,color,label in [(-.14,'kappa6','#2864A5','六级 κ'),(.14,'kappa3','#D4A12A','三阶 κ')]:
        ys=[a[key] for a in agreement];bounds=np.array([a[key+'_cluster_interval95'] for a in agreement]);positions=xs+shift
        ax.errorbar(positions,ys,yerr=np.maximum(0,np.array([ys-bounds[:,0],bounds[:,1]-ys])),fmt='o',color=color,capsize=5,markersize=9,label=label)
        for i,(x,y) in enumerate(zip(positions,ys)):ax.text(x,bounds[i,1]+.035,f'{y:.3f}',ha='center',fontsize=11)
    ax.set(xticks=xs,xticklabels=[f'DeepSeek / GLM\n双非空 n={agreement[0]["dual_nonnull"]}',f'Claude / GPT\n双非空 n={agreement[1]["dual_nonnull"]}'],ylim=(-.05,1.12),ylabel='条件一致性 κ')
    ax.axhline(0,color='#b3b8c2',lw=1);ax.legend(frameon=False,loc='upper center',bbox_to_anchor=(.5,1.2),ncols=2)
    save(fig,'02-model-kappa','六级与三阶：条件模型一致性','agreement.json','学生整簇 bootstrap 95% 区间；三阶=L1–2/L3–4/L5–6。复核子集不同，不用于模型排名。')
    fig,axes=plt.subplots(1,2,figsize=(12,6))
    for ax,key,label,mult,maximum in [(axes[0],'ABL','候选文本平均任务层级',1,6),(axes[1],'HOT','候选文本高阶任务占比（%）',100,100)]:
        vals=[terms['terms'][t][key]*mult for t in ('25f','26s')]
        bounds=np.array([terms['terms'][t][key+'_cluster_interval95'] for t in ('25f','26s')])*mult
        ax.bar([0,1],vals,color=['#2864A5','#D4A12A'],width=.55)
        ax.errorbar([0,1],vals,yerr=np.maximum(0,np.array([vals-bounds[:,0],bounds[:,1]-vals])),fmt='none',ecolor='#263442',capsize=5)
        ax.set(xticks=[0,1],xticklabels=['秋：90 条候选','春：97 条候选'],ylim=(0,maximum),ylabel=label)
        for x,y in enumerate(vals):ax.text(x,bounds[x,1]+.04*maximum,f'{y:.2f}',ha='center')
    save(fig,'03-semester-descriptive','两个学期的已可判定文本描述','semester-descriptions.json','来源：秋春各抽 192 条；误差线为学生聚类 95% 区间。仅条件描述，不能识别 AI 学习效应。')
    fig,ax=plt.subplots(figsize=(12,6));strategies=cost['strategies'];ys=[s['rmse'] for s in strategies]
    ax.barh([2,1,0],ys,color=['#A7B0BD','#6C91B8','#2864A5'])
    ax.set(yticks=[2,1,0],yticklabels=['等量分配','按规模分配','方差/成本最优'],xlabel='总体均值估计 RMSE（越小越好）',xlim=(0,max(ys)*1.28))
    for y,v,s in zip([2,1,0],ys,strategies):ax.text(v+.001,y,f'{v:.4f}；成本 {s["cost_units"]}',va='center')
    save(fig,'04-cost-allocation-simulation','模拟：同预算的抽样分配比较','cost-allocation-simulation.json','合成有限总体 N=400，每策略 500 次；预算 150 假定成本单位。最优策略使用已知残差方差。')
    fig,ax=plt.subplots(figsize=(12,6));pairs=list(weight_info['comparisons']);vals=[p['max_absolute_rank_change'] for p in pairs]
    ax.barh(np.arange(len(pairs)),vals,color='#2864A5');ax.set(yticks=np.arange(len(pairs)),yticklabels=[p['label'] for p in pairs],xlabel='学生名次最大绝对变化',xlim=(0,max(vals+[1])+2))
    for i,v in enumerate(vals):ax.text(v+.12,i,str(v),va='center')
    save(fig,'05-weight-rank-simulation','模拟：三套权重带来的排名变化','synthetic-weight-comparison.json','24 名合成学生、每人 8 条已知标签；固定三套权重。此图不是对真实学生的排名。')
    fig,ax=plt.subplots(figsize=(12,6));sub=red[red.scenario.isin(['工具堆叠','逐轮复制','真实改善对照'])];y=np.arange(len(sub))
    ax.barh(y+.16,sub.delta,height=.3,color='#2864A5',label='均衡 AIV');ax.barh(y-.16,sub.guarded_delta,height=.3,color='#D4A12A',label='MAB 不计分对照')
    ax.set(yticks=y,yticklabels=sub.scenario,xlabel='相对基线分数变化（分）');ax.axvline(0,color='#353E4B',lw=1);ax.legend(frameon=False)
    save(fig,'06-redteam-score','合成红队：工具堆叠与复制攻击','synthetic-redteam.csv','固定标签机制检验；MAB 零权重能消除该工具堆叠增分，同时失去对工具广度的奖励。')
    write(out/'figure-manifest.json',catalog)


def main(plan_path,out):
    started=time.time();out.mkdir(parents=True,exist_ok=True)
    manifest_path=plan_path.parent/'handoff-manifest.json';manifest=json.loads(manifest_path.read_text('utf-8'))
    from aiv.notebook_materials import load_frozen_batch
    summary=load_frozen_batch(manifest_path)
    source=ROOT/manifest['agent_batch']['annotations_path'];all_rows=json.loads(source.read_text('utf-8'))
    rows=[r for r in all_rows if r['record']['term']!='synthetic']
    agreements=[paired_agreement(rows,pair) for pair in [('deepseek-v4.1-flash','glm-5.3-flash'),('claude-sonnet-5','gpt-6-sol')]]
    write(out/'agreement.json',agreements)
    students=student_table(rows);students.to_csv(out/'student-indicators.csv',index=False,encoding='utf-8-sig')
    terms=semester_descriptions(rows)
    terms['partition_details']={p:semester_descriptions([r for r in rows if r['record']['partition']==p]) for p in ('regression','new_student')}
    write(out/'semester-descriptions.json',terms)
    association=lagged_process_association([dict(r['record'],label=r['final']) for r in rows]);write(out/'process-association.json',association)
    cost,trials=cost_experiment();write(out/'cost-allocation-simulation.json',cost);trials.to_csv(out/'cost-simulation-trials.csv',index=False)
    synthetic=synthetic_records(seed=26,students=24);scores=student_scores(synthetic)
    scores.to_csv(out/'synthetic-student-scores.csv',index=False,encoding='utf-8-sig')
    sensitivity=weight_sensitivity(scores,draws=1000);sensitivity.to_csv(out/'synthetic-weight-sensitivity.csv',index=False)
    ranks=scores[list(WEIGHTS)].rank(ascending=False,method='average');comparisons=[]
    for name,label in [('higher_order','均衡 vs 高阶优先'),('process','均衡 vs 过程优先')]:
        comparisons.append({'label':label,'scheme':name,'max_absolute_rank_change':float((ranks[name]-ranks.balanced).abs().max()),
            'spearman_rank_correlation':float(ranks.balanced.corr(ranks[name]))})
    comparison={'source':'synthetic_24_students_192_labels','comparisons':comparisons,'weights':{k:v.tolist() for k,v in WEIGHTS.items()},
        'max_rank_perturbation_width':float((sensitivity.rank_high-sensitivity.rank_low).max()),
        'component_correlations':json.loads(scores[list(NAMES)].corr().to_json()),
        'nonlinear_sqrt_sensitivity_max_score_change':float(np.abs(100*np.sqrt(scores[list(NAMES)].to_numpy())@WEIGHTS['balanced']-scores.balanced).max())}
    write(out/'synthetic-weight-comparison.json',comparison)
    causal_simulation().to_csv(out/'synthetic-causal-simulation.csv',index=False)
    attacks=redteam();attacks.to_csv(out/'synthetic-redteam.csv',index=False,encoding='utf-8-sig')
    score_availability={'student_term_rows':len(students),'labeled_student_term_rows':int((students.candidate_records>0).sum()),
        'full_AIV_available':int(students.AIV_balanced.notna().sum()),'full_AIV_rank_available':int(students.rank_balanced.notna().sum()),
        'session_edges_identifiable':False,'spring_tool_identity_available':False,
        'reason':'All session boundaries missing; spring tool identities missing. Formula ranges condition on observed candidate labels, and are not confidence intervals or rankings.'}
    write(out/'score-availability.json',score_availability)
    figures(out,rows,agreements,terms,cost,comparison,attacks)
    metadata={'source_annotations':str(source.relative_to(ROOT)).replace('\\','/'),'source_sha256':sha(source),
        'seed':26,'no_model_calls':True,'real_records':len(rows),'student_term_rows':len(students),
        'independent_human_reference_available':False,'elapsed_seconds':time.time()-started,
        'software':{'numpy':np.__version__,'pandas':pd.__version__},
        'reproduce':'.venv/Scripts/python.exe scripts/build_solution_materials.py',
        'files':{str(p.relative_to(out)).replace('\\','/'):sha(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name not in ('manifest.json','figure-contact-sheet.png')},
        'code_hashes':{str(p.relative_to(ROOT)).replace('\\','/'):sha(p) for p in [Path(__file__),ROOT/'aiv/solution_materials.py',ROOT/'aiv/metrics.py',ROOT/'aiv/calibration.py',ROOT/'aiv/analysis.py',ROOT/'aiv/association.py']}}
    write(out/'manifest.json',metadata)
    print(json.dumps({'output':str(out),'seconds':metadata['elapsed_seconds'],'score_availability':score_availability,'association':association,'agreements':[{k:v for k,v in a.items() if not k.startswith('matrix')} for a in agreements]}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--plan',type=Path,default=ROOT/DEFAULT)
    parser.add_argument('--out',type=Path,default=ROOT/'runtime/research/solution-materials-384-v1')
    args=parser.parse_args();target=args.out.resolve()
    if not target.is_relative_to(ROOT/'runtime/research'):raise SystemExit('Student-level exports must stay in private runtime/research')
    main(args.plan.resolve(),target)
