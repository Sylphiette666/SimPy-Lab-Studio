"""Auditable analysis of saved replications; no changes to simulation semantics."""
from __future__ import annotations

import math
import statistics
METRICS = ('throughput_per_hour', 'avg_wip', 'specific_energy_kwh_per_part')
STATES = ('processing', 'starved', 'blocked', 'failed', 'off_shift')


def finite(value):
    return isinstance(value, (float,int)) and not isinstance(value,bool) and math.isfinite(value)


def mean(values):
    good = [v for v in values if finite(v)]
    return statistics.fmean(good) if good else None


def diagnostics(result: dict, preview: dict | None = None) -> dict:
    config = result['config']; records = result.get('replications', [])
    seconds = config['until_seconds'] - config['warmup_seconds']
    machines = []
    for spec in config['machines']:
        fractions = {key:mean([r['metrics']['machine'][spec['name']]['state_fractions'][key] for r in records]) for key in STATES}
        machines.append({'name':spec['name'], 'fractions':fractions,
                         'hours':{key:(v * seconds / 3600 if v is not None else None) for key,v in fractions.items()}})
    ranking = sorted(machines,key=lambda item:item['fractions']['processing'] or 0,reverse=True)
    buffers = []
    for spec in config['buffers']:
        occupancy = mean([r['metrics']['buffer'][spec['name']]['avg_occupancy'] for r in records])
        buffers.append({'name':spec['name'],'capacity':spec['capacity'],'average_occupancy':occupancy,
                        'fill_ratio':occupancy / spec['capacity'] if occupancy is not None else None})
    heat = []
    if preview:
        frames = [f for f in preview.get('frames',[]) if f['time_seconds'] >= preview['warmup_seconds']]
        stride = max(1,math.ceil(len(frames) / 60))
        for index,spec in enumerate(config['buffers']):
            cells = []
            for start in range(0,len(frames),stride):
                chunk = frames[start:start+stride]
                cells.append({'time_seconds':chunk[0]['time_seconds'],
                              'fill_ratio':mean([f['buffers'][index]['level'] / f['buffers'][index]['capacity'] for f in chunk])})
            heat.append({'name':spec['name'],'cells':cells})
    replication_heat = [{'name':spec['name'], 'cells':[{'replication':r['replication']+1,
                         'fill_ratio':r['metrics']['buffer'][spec['name']]['avg_occupancy']/spec['capacity']}
                         for r in records]} for spec in config['buffers']]
    return {'machines':machines,'buffers':buffers,'heatmap':heat,'replication_heatmap':replication_heat,
            'bottleneck_candidates':[m['name'] for m in ranking[:2]],
            'replications':len(records),'observation_seconds':seconds,
            'explanation':'候选按加工时间占比排序；结合上游堵塞、下游缺料和故障占比判断。高加工占比是瓶颈线索，不证明因果；请通过同条件参数实验验证。',
            'heatmap_note':'热力图来自本方案单次预览的预热后定时采样，可能截短；不是完整重复实验的精确空/满持续时间。'}


def paired_comparison(left: dict, right: dict) -> dict:
    """Exact two-sided paired sign test, Bonferroni correction across 3 KPIs.

    Tests the sign balance of paired differences, not equality of mean magnitudes.
    NIST: https://www.itl.nist.gov/div898/software/dataplot/refman1/auxillar/signtest.htm
    """
    keys = ('until_seconds','warmup_seconds','replications','base_seed','confidence_level','breaks')
    reasons = [key for key in keys if left['config'].get(key) != right['config'].get(key)]
    if left.get('random_streams') != right.get('random_streams'):
        reasons.append('random_streams')
    lr,rr = left.get('replications',[]),right.get('replications',[])
    left_keys = [(r.get('replication'),r.get('seed')) for r in lr]
    right_keys = [(r.get('replication'),r.get('seed')) for r in rr]
    if len(set(left_keys)) != len(left_keys) or set(left_keys) != set(right_keys) or len(lr) != len(rr):
        reasons.append('replication_seeds')
    if reasons: return {'comparable':False,'reasons':reasons,'metrics':[]}
    left_by_key = dict(zip(left_keys,lr)); right_by_key = dict(zip(right_keys,rr))
    alpha = 1 - left['config']['confidence_level']; results = []
    for metric in METRICS:
        pairs = [(left_by_key[k]['metrics'].get(metric),right_by_key[k]['metrics'].get(metric)) for k in left_keys]
        differences = [b-a for a,b in pairs if finite(a) and finite(b)]
        positive = sum(d > 0 for d in differences); negative = sum(d < 0 for d in differences)
        n = positive + negative
        p = min(1.0, 2 * sum(math.comb(n,i) for i in range(min(positive,negative)+1)) / 2**n) if n else 1.0
        adjusted = min(1.0,p * len(METRICS))
        results.append({'metric':metric,'pairs':len(differences),'missing_pairs':len(pairs)-len(differences),
                        'ties':len(differences)-n,'positive':positive,'negative':negative,
                        'mean_difference':mean(differences),
                        'median_difference':statistics.median(differences) if differences else None,
                        'p_value':p,'adjusted_p_value':adjusted,
                        'significant':n > 0 and len(differences)==len(pairs) and adjusted <= alpha})
    return {'comparable':True,'metrics':results,'alpha':alpha,
            'method':'双侧精确配对符号检验；3 项指标使用 Bonferroni 校正；差值 = 待评方案 − 对照。',
            'note':'检验关注重复实验中差异的方向，不检验均值大小。零差值不参与符号计数；缺失配对时不下显著结论。未显著不等于等效，筛选后的方案需独立种子验证。'}


def precision(result: dict, relative: float = .05, absolute: dict | None = None) -> list[dict]:
    if not finite(relative) or not 0.001 <= relative <= 1:
        raise ValueError('相对误差目标须在 0.1%–100% 之间。')
    z = statistics.NormalDist().inv_cdf((1 + result['config']['confidence_level']) / 2)
    rows = []
    for metric in METRICS:
        values = [r['metrics'].get(metric) for r in result.get('replications',[])]
        good = [v for v in values if finite(v)]; average = mean(good)
        deviation = statistics.stdev(good) if len(good) > 1 else None
        tolerance = (absolute or {}).get(metric) or (abs(average) * relative if average else None)
        required = max(2, math.ceil((z * deviation / tolerance) ** 2)) if tolerance and deviation is not None else None
        rows.append({'metric':metric,'n':len(good),'missing':len(values)-len(good), 'mean':average,
                     'target_half_width':tolerance,'estimated_total':required,
                     'additional':max(0,required-len(good)) if required is not None else None,
                     'exceeds_limit':required is not None and required > 50,
                     'note':'基于先导样本标准差和正态近似；不是精度保证。零均值或不足 2 次时需更多先导实验。'})
    return rows


def calibrate(result: dict, reference: dict[str, float]) -> dict:
    """Compare user-confirmed same-condition measurements, without fitting engine code."""
    rows, scores = [], []
    for metric,target in reference.items():
        if metric not in METRICS or not finite(target) or target <= 0:
            raise ValueError('实测参照须为三个主要指标的正数；请核对单位与观测条件。')
        simulated = mean([r['metrics'].get(metric) for r in result.get('replications',[])])
        relative = (simulated-target) / target if simulated is not None else None
        if relative is not None: scores.append(relative**2)
        rows.append({'metric':metric,'observed':target,'simulated':simulated,'relative_error':relative})
    return {'rows':rows,'normalized_rmse':math.sqrt(statistics.fmean(scores)) if scores and len(scores)==len(reference) else None,
            'note':'同单位、同观测时段的实测参照。误差小不证明模型有效；可在批量实验中筛选参数，再用独立实测数据验证。'}
