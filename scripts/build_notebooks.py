"""Build seven public teaching notebooks; no competition rows or live calls by default."""
from pathlib import Path
import nbformat as nbf
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'notebooks';OUT.mkdir(exist_ok=True)
setup='''from pathlib import Path
import sys, json
ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / 'aiv').is_dir())
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
plt.rcParams.update({'figure.figsize': (8, 4), 'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False})
from aiv.data import synthetic_records
records = synthetic_records(seed=26)
LIVE_API = False  # Explicit opt-in only. Default cells never spend credits.
'''
def make(number,title,goal,steps,checks,next_steps):
    nb=nbf.v4.new_notebook();nb.metadata['kernelspec']={'name':'python3','display_name':'Python 3','language':'python'}
    cells=[nbf.v4.new_markdown_cell(f'# {number}. {title}\n\n## Goal\n{goal}\n\n**数据来源：** `aiv/data.py` 的确定性合成样本。不是竞赛原始数据或真实教育效果证据。\n\n## Setup\n在项目环境中从干净内核运行全部单元。默认不读取 .env、不调用 API。'),nbf.v4.new_code_cell(setup)]
    for heading,code,meaning in steps:
        cells += [nbf.v4.new_markdown_cell('## '+heading),nbf.v4.new_code_cell(code),nbf.v4.new_markdown_cell(meaning)]
    cells += [nbf.v4.new_markdown_cell('## Checks'),nbf.v4.new_code_cell(checks),nbf.v4.new_markdown_cell('## Next Steps\n'+next_steps)]
    nb.cells=cells;nbf.validate(nb);nbf.write(nb,OUT/f'{number:02d}_{title}.ipynb')

make(1,'数据与问题','把 A–E 连成证据链，并区分观测单位、缺失与不可识别。',[
('数据字典与粒度',"frame = pd.DataFrame(records)\ndisplay(frame[['student','timestamp','question','agent','label']].head(6))\ndisplay(pd.DataFrame({'field': ['student','label','session','agent'], 'meaning': ['合成学生编号','构造任务层级 1–6','已知合成会话边界','合成工具类型']}))",'一行是一次提问；任务要求层级不等于学生掌握程度。合成会话边界已知，真实 CSV 缺失边界时不能直接串联。'),
('检查分布',"counts = frame.label.value_counts().reindex(range(1,7),fill_value=0)\ncounts.plot.bar(color='#137c80',rot=0)\nplt.xlabel('Constructed Bloom level'); plt.ylabel('Number of records'); plt.title('Synthetic records: level distribution'); plt.tight_layout(); plt.show()",'六级分布来自固定随机种子；不能把此分布作为真实学生的能力分布。')],
"assert len(frame)==192 and frame.student.nunique()==24\nassert frame.id.is_unique\nassert frame.label.between(1,6).all()",'阅读题面 A–E 与 `docs/标注与人工校验手册.md`，列出你希望补采的独立学习结果。')
make(2,'标注与互评','学习证据匹配、弃权、独立判断与一次匿名互评。',[
('结构化证据校验',"from aiv.annotation import parse_judgment, majority\nquestion = '我比较了两种假设的适用条件。'\nexample = dict(level=4, probabilities=[0,0,.1,.8,.1,0], evidence='比较了两种假设', reason='任务涉及假设比较', contribution_level=4, contribution_evidence='比较了两种假设', uncertain=True)\nparsed = parse_judgment(json.dumps(example), question)\ndisplay(pd.DataFrame([parsed]).drop(columns='probabilities'))",'这是教学示例。原文匹配只能排除虚构引文，不能证明标签或推理正确。'),
('重放公开合成对照缓存',"cache_path = ROOT / 'examples' / 'synthetic-judge-cache.json'\ncache = json.loads(cache_path.read_text('utf-8')) if cache_path.exists() else []\ncomparison = pd.DataFrame([{'case': x['record']['id'], 'constructed': x['record']['label'], 'single': x['single_model'], 'vote': x['vote'], 'peer': x['peer_vote'], 'final': x['final']} for x in cache])\ndisplay(comparison if len(comparison) else pd.DataFrame({'status':['尚无经核验的公开缓存']}))",'缓存若存在，来自模型对合成对照的实际调用。构造标签不是独立人工金标准；真实准确率只能用盲审子集评估。')],
"assert majority([{'level':4},{'level':4},None]) is None\nassert LIVE_API is False",'实时调用需显式运行 `scripts/run_annotation.py`；记录模型、服务、提示版本和重复编号。不要把 key 数量当模型家族数。')
make(3,'人审与校准','理解固定预算下的分层残差校正和适用条件。',[
('固定预算分配',"from aiv.calibration import allocate_review, residual_mean, agreement\nallocation = allocate_review([.5,.3,.2],[.1,.3,.5],[1,2,3],budget=70)\ndisplay(pd.DataFrame({'stratum':['low','medium','high'],'n':allocation['allocation']}))\nprint('Person-minute cost:',allocation['cost'])",'这里的标准差与成本是教学参数。实际分配必须用试标耗时和残差估计；70 分钟对应辅助复核阶段，不冒充独立准确性评估预算。'),
('残差校正模拟',"rng=np.random.default_rng(26)\ntruth=rng.binomial(1,.45,400); prediction=np.clip(truth*.6+.3+rng.normal(0,.15,400),0,1)\nidx=rng.choice(400,50,replace=False)\ncorrected=residual_mean(prediction,idx,truth[idx])\ndisplay(pd.DataFrame({'estimate':[truth.mean(),prediction.mean(),corrected['estimate']]},index=['known truth','prediction only','residual corrected']))\nplt.bar(['Truth','Predicted','Corrected'],[truth.mean(),prediction.mean(),corrected['estimate']],color=['#8b99a9','#b6955b','#137c80']);plt.ylim(0,1);plt.ylabel('Synthetic mean');plt.tight_layout();plt.show()",'有限总体简单随机抽样保证残差估计无偏。小样本正态区间只是近似；同学生多条相关记录需要按学生设计抽样或聚类方差。')],
"assert allocation['cost'] <= 70\ncensus=residual_mean(prediction,np.arange(400),truth)\nassert abs(census['estimate']-truth.mean())<1e-10",'用 A、B 双人盲审评估一致性；辅助复核另统计。把 120 人分钟拆成 30+70+20，模型互评不能替代真实人审。')
make(4,'识别边界与模拟','演示混杂如何影响估计，避免把调整回归自动称为因果。',[
('已知真值实验',"from aiv.analysis import causal_simulation\nresult=causal_simulation()\ndisplay(result.round(4))\ny=np.arange(len(result));plt.errorbar(result.estimate,y,xerr=[result.estimate-result.lower,result.upper-result.estimate],fmt='o',color='#137c80',capsize=5);plt.axvline(.4,color='#a65f22',linestyle='--',label='Known truth = 0.4');plt.yticks(y,result.model);plt.xlabel('Synthetic treatment coefficient with 95% interval');plt.legend();plt.tight_layout();plt.show()",'真效应设为 0.4，调整模型纳入了生成机制中的真实混杂因素。真实数据中不可验证不存在遗漏混杂，因而不能照搬此结论。'),
('真实数据识别检查表',"display(pd.DataFrame({'requirement':['独立前测/后测','可比处理组与对照组','处理前协变量','跨学期共同锚定任务','学生聚类与时间顺序'],'current_status':['未发现','未建立','有限','未发现','部分可用']}))",'现阶段应报告使用方式与后续可观察过程的关联。对 AI 的真实学习增量、成熟效应与课程差异，缺少独立信息时结论是不可识别。')],
"assert result.source.eq('synthetic_known_truth').all()\nassert result.true_effect.eq(.4).all()",'新增随机或准实验设计、共同任务和无AI迁移测验；预先定义处理、结果、协变量与泄漏检查。')
make(5,'指标性质与不确定性','计算 ABL/HOT/CTQ/DHI/MAB，并观察三套权重与标签误差的影响。',[
('三套固定权重',"from aiv.analysis import student_scores, weight_sensitivity\nfrom aiv.metrics import metrics, composite, dhi, IDEAL, transition_quality\nscores=student_scores(records)\ndisplay(scores[['student','balanced','higher_order','process']].head(10).round(2))\nscores[['balanced','higher_order','process']].plot.box();plt.ylabel('Synthetic AIV / 100');plt.title('Three fixed weight schemes');plt.tight_layout();plt.show()",'权重代表预先指定的教育侧重点；理想分布是设计假设，没有被当作实证最优值。'),
('权重敏感性',"sensitivity=weight_sensitivity(scores)\ndisplay(sensitivity.head(8).round(2))\nplt.scatter(sensitivity.rank_low,sensitivity.rank_high,color='#137c80');plt.xlabel('2.5% rank quantile');plt.ylabel('97.5% rank quantile');plt.title('Synthetic ranking under ±10% normalized weights');plt.tight_layout();plt.show()",'排名分位范围由固定扰动机制得到，不是经覆盖率校验的统计置信区间。标签误差的传播还依赖联合错误结构。')],
"assert dhi(IDEAL)==1\nassert transition_quality([2,3],[None,None]) is None\nassert scores.balanced.between(0,100).all()",'阅读 `docs/指标定义与性质.md`；测试 CTQ 对复制/分段的敏感性与 MAB 的工具堆叠漏洞。')
make(6,'红队与失败边界','用攻击与正向对照寻找评价公式的可操纵性。',[
('固定标签攻击',"from aiv.analysis import redteam\nattacks=redteam();display(attacks[['scenario','score','delta','CTQ','MAB_raw']].round(3))\nlabels=['Baseline','Repeat block','Repeat turns','Tool stacking','Split sessions','Improvement']\nplt.bar(labels,attacks.score,color='#137c80');plt.xticks(rotation=25,ha='right');plt.ylabel('Synthetic AIV / 100');plt.title('Missing score means unidentifiable, not zero');plt.tight_layout();plt.show()",'当前公式对工具堆叠有明显增分；逐轮拆分后 CTQ 无法识别而弃权。展示失败比宣称全面鲁棒更有用。'),
('文本级攻击协议',"display(pd.DataFrame({'attack':['冗长扩写','认知动词注入','复制AI答案','真实改善对照'],'expected':['不凭长度升级','不凭关键词升级','不把AI产出归于学生','保留有证据的升级'],'validation':['需调用相同模型并用人审真值']*4}))",'本单元的固定标签实验没有验证文本级模型鲁棒性。正式实验应同预算、同模型、同评估条件比较。')],
"assert attacks.loc[attacks.scenario=='分段操纵','score'].isna().all()\nassert attacks.loc[attacks.scenario=='工具堆叠','delta'].iloc[0]>0",'把失败案例纳入报告；工具广度宜单独呈现机会条件，避免直接用不同可用工具集合进行跨班比较。')
make(7,'教育报告与复用','为学生、教师、管理者生成证据范围内的行动建议。',[
('三类教育报告',"from aiv.analysis import educational_report\nreports=[educational_report(records,role) for role in ['student','teacher','administrator']]\ndisplay(pd.DataFrame(reports)[['role','judgment','action']])",'所有报告明示独立学习效果不可判断。建议补采的字段直接对应识别缺口。'),
('复现与导出',"manifest={'seed':26,'records':len(records),'live_api':LIVE_API,'source':'synthetic','excluded':['原始学生记录','未获再分发许可的文献全文','凭据','私有仓库历史']}\ndisplay(pd.DataFrame({'field':manifest.keys(),'value':[str(v) for v in manifest.values()]}))",'交卷后通过白名单导出建立独立公开版本。公开包只含教学源码、合成样本、可发布缓存与环境说明。')],
"assert all(r['source']=='synthetic' for r in reports)\nassert not LIVE_API",'从干净内核依次执行七个单元；使用共享计算函数扩展到你自己的合规数据，并重新验证标签和识别假设。')
print('Created 7 teaching notebooks')
