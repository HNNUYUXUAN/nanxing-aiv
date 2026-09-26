"""Prospective process association, never an identified learning-effect estimator."""
import numpy as np
import pandas as pd


def lagged_process_association(records, minimum_students=30):
    frame = pd.DataFrame(records)
    if frame.empty or not {'student','term','timestamp','label'} <= set(frame):
        return {'status':'unavailable','reason':'missing longitudinal fields'}
    frame['time']=pd.to_datetime(frame.timestamp,errors='coerce')
    frame=frame[frame.label.notna() & frame.time.notna()].copy()
    rows=[]
    for (student,term),group in frame.groupby(['student','term']):
        history=[]
        # Equal timestamps are not arbitrarily ordered as past and future.
        for stamp,at_time in group.groupby('time',sort=True):
            if history:
                for _,row in at_time.iterrows():
                    rows.append({'student':student,'term':term,'day':stamp.toordinal(),
                        'prior_log_count':np.log1p(len(history)),'prior_hot':np.mean(np.array(history)>=4),
                        'outcome_hot':float(row.label>=4)})
            history.extend(at_time.label.astype(int).tolist())
    data=pd.DataFrame(rows)
    n_students=data.student.nunique() if len(data) else 0
    if n_students<minimum_students or len(data)<100:
        return {'status':'unavailable','reason':'insufficient labeled prospective records',
                'eligible_records':len(data),'student_clusters':int(n_students),
                'minimum_students':minimum_students,'minimum_records':100}
    import statsmodels.api as sm
    x=pd.concat([data[['prior_log_count','prior_hot']],pd.get_dummies(data.term,prefix='term',drop_first=True,dtype=float)],axis=1)
    x['time_days']=data.day-data.day.min();x=sm.add_constant(x)
    if np.linalg.matrix_rank(x.to_numpy(dtype=float))<x.shape[1]:
        return {'status':'unavailable','reason':'rank deficient design'}
    fit=sm.OLS(data.outcome_hot,x).fit(cov_type='cluster',cov_kwds={'groups':data.student})
    ci=fit.conf_int().loc['prior_log_count']
    return {'status':'estimated_association_only','records':len(data),'student_clusters':int(n_students),
        'coefficient':float(fit.params['prior_log_count']),'interval95':ci.tolist(),'p_value':float(fit.pvalues['prior_log_count']),
        'limitations':['No independent learning outcome or causal exchangeability guarantee.',
                      'Cluster identity follows supplied identifier; cross-term stability needs verification.',
                      'Label measurement error and informative record absence remain.',
                      'Prior count may proxy opportunity and elapsed participation.'],
        'bias_sensitivity':[{'assumed_bias':b,'adjusted_coefficient':float(fit.params['prior_log_count']-b)} for b in [-.2,-.1,0,.1,.2]]}
