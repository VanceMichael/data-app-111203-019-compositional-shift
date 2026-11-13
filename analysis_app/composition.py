"""两期组成分布比较。

本模块在闭合约束下比较两个 period 的组成分布：

- 先验证非负、闭合容差与批次完整性，再进入任何几何计算；
- 零值必须由用户声明策略处理（检测限以下 / 结构性不存在），不做
  “统一加一个小数再取对数”这类未声明的隐含假定；
- 用 CLR 表示与 Aitchison 距离保持组成几何关系，置换检验以批次为重采样
  单元，保留 supplier 内批次结构；
- lot_size 物质量加权与每批等权分别成块输出，绝不合并为同一统计总体；
- 结果把供应结构变动与同供应方内部配比变动分开，缺少共同供应方的部分
  单独列为不可分离。

输出是质量监测线索，不是不合格判定。
"""

import math
import random
from collections import defaultdict
from dataclasses import dataclass

from .io import fingerprint
from .schema import COMPONENTS, ENTITY, GROUP, PRODUCT, UNIT

ZERO_POLICIES = ('detection_limit', 'structural', 'reject')
WEIGHTINGS = ('equal_batch', 'mass')
WEIGHTING_LABELS = {'equal_batch': '每批等权', 'mass': 'lot_size 物质量加权'}

REASON_LABELS = {
    'negative_component': '组分份额为负',
    'zero_total': '三组分全为零，批次无组成信息',
    'closure_tolerance': '闭合误差超出容差',
    'invalid_lot_size': 'lot_size 非正',
    'invalid_detection_limit': 'detection_limit 非正',
    'batch_spans_supplier_or_period': '同一批次跨供应方或比较期',
    'zero_replacement_impossible': '按检测限替换后份额无法闭合',
    'structural_subcomposition_empty': '剔除结构性零组分后子组成无正值',
}

_EPS = 1e-12


@dataclass(frozen=True)
class Config:
    closure_tolerance: float = 1e-6
    zero_policy: str = 'detection_limit'
    zero_fraction: float = 0.5
    sensitivity_fractions: tuple = (0.1, 0.25, 0.5, 0.65, 1.0)
    permutations: int = 999
    seed: int = 20260101
    alpha: float = 0.05
    baseline_period: int | None = None
    current_period: int | None = None


def check_config(config):
    if not math.isfinite(config.closure_tolerance) or config.closure_tolerance < 0:
        raise ValueError('闭合容差必须是非负有限数')
    if config.zero_policy not in ZERO_POLICIES:
        raise ValueError('未知零值策略：' + str(config.zero_policy))
    if not 0 < config.zero_fraction <= 1:
        raise ValueError('零值替换比例必须在 (0, 1]')
    if not config.sensitivity_fractions:
        raise ValueError('敏感性比例列表不能为空')
    for fraction in config.sensitivity_fractions:
        if not 0 < fraction <= 1:
            raise ValueError('敏感性比例必须在 (0, 1]')
    if config.permutations < 1:
        raise ValueError('置换次数必须为正')
    if not 0 < config.alpha < 1:
        raise ValueError('显著性水平必须在 (0, 1)')
    if (config.baseline_period is None) != (config.current_period is None):
        raise ValueError('baseline 与 current 比较期需同时给出')
    if (config.baseline_period is not None
            and config.baseline_period == config.current_period):
        raise ValueError('baseline 与 current 比较期不能相同')


def _norm(vector):
    return math.sqrt(math.fsum(value * value for value in vector))


def _clr(values):
    logs = [math.log(value) for value in values]
    center = math.fsum(logs) / len(logs)
    return tuple(value - center for value in logs)


def _wmean(vectors, weights):
    total = math.fsum(weights)
    return tuple(math.fsum(w * v[i] for w, v in zip(weights, vectors)) / total
                 for i in range(len(vectors[0])))


def _geometric_shares(clr):
    shift = max(clr)
    weights = [math.exp(value - shift) for value in clr]
    total = math.fsum(weights)
    return [value / total for value in weights]


def _safe_fold(log2_fold):
    if -1022.0 <= log2_fold <= 1023.0:
        return 2.0 ** log2_fold
    return None


def _screen(ordered, config):
    """行级验证：非负、闭合容差与批次完整性。返回通过行与验证信息。"""
    batch_scopes = defaultdict(set)
    for row in ordered:
        batch_scopes[row[ENTITY]].add((row[GROUP], row['period']))
    invalid_batches = sorted(batch for batch, scopes in batch_scopes.items()
                             if len(scopes) > 1)
    invalid_batch_set = set(invalid_batches)

    excluded = defaultdict(list)
    screened = []
    zero_counts = {component: 0 for component in COMPONENTS}
    below_limit_positive = {component: 0 for component in COMPONENTS}
    nonnegative_violations = 0
    all_zero_rows = 0
    max_closure_error = 0.0
    max_closure_error_used = 0.0

    for row in ordered:
        values = {component: row[component] for component in COMPONENTS}
        total = math.fsum(values.values())
        closure_error = abs(total - 1.0)
        max_closure_error = max(max_closure_error, closure_error)
        reason = None
        if row[ENTITY] in invalid_batch_set:
            reason = 'batch_spans_supplier_or_period'
        elif any(value < 0 for value in values.values()):
            nonnegative_violations += 1
            reason = 'negative_component'
        elif total <= 0:
            all_zero_rows += 1
            reason = 'zero_total'
        elif closure_error > config.closure_tolerance:
            reason = 'closure_tolerance'
        elif row['lot_size'] <= 0:
            reason = 'invalid_lot_size'
        elif row['detection_limit'] <= 0:
            reason = 'invalid_detection_limit'
        if reason is not None:
            excluded[reason].append(row['observation_id'])
            continue
        max_closure_error_used = max(max_closure_error_used, closure_error)
        zeros = tuple(component for component in COMPONENTS
                      if values[component] == 0)
        for component in zeros:
            zero_counts[component] += 1
        for component in COMPONENTS:
            value = values[component]
            if 0 < value < row['detection_limit']:
                below_limit_positive[component] += 1
        screened.append({
            'row': row,
            'values': tuple(values[component] / total
                            for component in COMPONENTS),
            'zeros': zeros,
        })

    validation = {
        'rows_total': len(ordered),
        'rows_screened': len(screened),
        'rows_excluded': sum(len(ids) for ids in excluded.values()),
        'excluded_by_reason': {
            reason: {'count': len(ids), 'label': REASON_LABELS[reason],
                     'examples': ids[:5]}
            for reason, ids in sorted(excluded.items())
        },
        'nonnegative_violations': nonnegative_violations,
        'all_zero_rows': all_zero_rows,
        'closure_tolerance': config.closure_tolerance,
        'max_closure_error': max_closure_error,
        'max_closure_error_used': max_closure_error_used,
        'zero_counts': zero_counts,
        'positive_below_detection_limit': below_limit_positive,
        'invalid_batches': invalid_batches,
    }
    return screened, validation


def _prepare(screened, config, fraction):
    """按零值策略构造 CLR 表示。失败时返回 (None, 原因)。"""
    if config.zero_policy == 'reject':
        zero_rows = sum(1 for item in screened if item['zeros'])
        if zero_rows:
            raise ValueError(
                f'检测到 {zero_rows} 行含零值，策略 reject 要求先声明零值成因'
                '（detection_limit 或 structural）')

    excluded = defaultdict(list)
    prepared = []
    imputed_zeros = 0

    if config.zero_policy == 'structural':
        used_components = tuple(
            component for component in COMPONENTS
            if all(item['values'][COMPONENTS.index(component)] > 0
                   for item in screened))
        excluded_components = tuple(
            component for component in COMPONENTS
            if component not in used_components)
        if len(used_components) < 2:
            return None, {
                'reason': 'structural_subcomposition_too_small',
                'excluded_components': list(excluded_components),
            }
        indices = [COMPONENTS.index(component) for component in used_components]
        for item in screened:
            subtotal = math.fsum(item['values'][index] for index in indices)
            if subtotal <= 0:
                excluded['structural_subcomposition_empty'].append(
                    item['row']['observation_id'])
                continue
            values = tuple(item['values'][index] / subtotal
                           for index in indices)
            prepared.append({'row': item['row'], 'values': values,
                             'clr': _clr(values)})
        return {
            'prepared': prepared,
            'used_components': used_components,
            'excluded_components': excluded_components,
            'imputed_zeros': 0,
            'excluded': excluded,
            'zero_fraction': None,
        }, None

    for item in screened:
        values = list(item['values'])
        zero_indices = [index for index, value in enumerate(values)
                        if value == 0]
        if zero_indices:
            delta = fraction * item['row']['detection_limit']
            if len(zero_indices) * delta >= 1.0:
                excluded['zero_replacement_impossible'].append(
                    item['row']['observation_id'])
                continue
            positive_total = math.fsum(value for value in values if value > 0)
            factor = (1.0 - len(zero_indices) * delta) / positive_total
            values = [delta if value == 0 else value * factor
                      for value in values]
            imputed_zeros += len(zero_indices)
        total = math.fsum(values)
        values = tuple(value / total for value in values)
        prepared.append({'row': item['row'], 'values': values,
                         'clr': _clr(values)})
    return {
        'prepared': prepared,
        'used_components': COMPONENTS,
        'excluded_components': (),
        'imputed_zeros': imputed_zeros,
        'excluded': excluded,
        'zero_fraction': fraction,
    }, None


def _build_units(prepared, weighting):
    """批次是重采样与加权的基本单元；supplier 内批次结构在此固定。"""
    groups = defaultdict(list)
    for item in prepared:
        groups[item['row'][ENTITY]].append(item)
    units = []
    for batch in sorted(groups):
        items = groups[batch]
        vectors = [item['clr'] for item in items]
        if weighting == 'mass':
            weights = [item['row']['lot_size'] for item in items]
            clr = _wmean(vectors, weights)
            weight = math.fsum(weights)
        else:
            clr = _wmean(vectors, [1.0] * len(items))
            weight = 1.0
        units.append({
            'batch': batch,
            'supplier': items[0]['row'][GROUP],
            'period': items[0]['row']['period'],
            'clr': clr,
            'weight': weight,
            'rows': len(items),
        })
    return units


def _period_centroids(units, periods):
    centroids = {}
    for period in periods:
        members = [unit for unit in units if unit['period'] == period]
        if members:
            centroids[period] = _wmean([unit['clr'] for unit in members],
                                       [unit['weight'] for unit in members])
    return centroids


def _supplier_table(units, periods):
    table = {}
    for period in periods:
        members = [unit for unit in units if unit['period'] == period]
        total_weight = math.fsum(unit['weight'] for unit in members)
        by_supplier = defaultdict(list)
        for unit in members:
            by_supplier[unit['supplier']].append(unit)
        for supplier, group in by_supplier.items():
            weight = math.fsum(unit['weight'] for unit in group)
            table[(supplier, period)] = {
                'weight_share': weight / total_weight,
                'clr': _wmean([unit['clr'] for unit in group],
                              [unit['weight'] for unit in group]),
                'batch_units': len(group),
            }
    return table


def _decompose(table, baseline, current, dimension):
    suppliers_baseline = {s for (s, p) in table if p == baseline}
    suppliers_current = {s for (s, p) in table if p == current}
    common = sorted(suppliers_baseline & suppliers_current)
    entered = sorted(suppliers_current - suppliers_baseline)
    exited = sorted(suppliers_baseline - suppliers_current)

    within = [0.0] * dimension
    structure = [0.0] * dimension
    non_separable = [0.0] * dimension
    for supplier in common:
        entry_b = table[(supplier, baseline)]
        entry_c = table[(supplier, current)]
        mean_weight = (entry_b['weight_share'] + entry_c['weight_share']) / 2.0
        delta_weight = entry_c['weight_share'] - entry_b['weight_share']
        mean_clr = _wmean([entry_b['clr'], entry_c['clr']], [0.5, 0.5])
        for index in range(dimension):
            within[index] += mean_weight * (entry_c['clr'][index]
                                            - entry_b['clr'][index])
            structure[index] += delta_weight * mean_clr[index]
    for supplier in entered:
        entry = table[(supplier, current)]
        for index in range(dimension):
            non_separable[index] += entry['weight_share'] * entry['clr'][index]
    for supplier in exited:
        entry = table[(supplier, baseline)]
        for index in range(dimension):
            non_separable[index] -= entry['weight_share'] * entry['clr'][index]

    separable = bool(common)
    common_weight_baseline = math.fsum(
        table[(s, baseline)]['weight_share'] for s in common)
    common_weight_current = math.fsum(
        table[(s, current)]['weight_share'] for s in common)

    def block(vector):
        return {'clr': list(vector), 'shift': _norm(vector)}

    return {
        'separable': separable,
        'common_suppliers': common,
        'entered_suppliers': entered,
        'exited_suppliers': exited,
        'common_weight_share_baseline': common_weight_baseline,
        'common_weight_share_current': common_weight_current,
        'within_supplier': block(within) if separable else None,
        'supplier_structure': block(structure) if separable else None,
        'non_separable': block(non_separable),
        'non_separable_note': (
            '进入/退出供应方的贡献无法区分为供应结构变动还是其内部配比变动'
            if (entered or exited) else None),
    }


def _ratio_changes(centroid_baseline, centroid_current, used_components):
    shares_baseline = _geometric_shares(centroid_baseline)
    shares_current = _geometric_shares(centroid_current)
    pairs = []
    for first in range(len(used_components)):
        for second in range(first + 1, len(used_components)):
            log2_baseline = math.log2(shares_baseline[first]
                                      / shares_baseline[second])
            log2_current = math.log2(shares_current[first]
                                     / shares_current[second])
            log2_fold = log2_current - log2_baseline
            pairs.append({
                'pair': used_components[first] + '/' + used_components[second],
                'log2_ratio_baseline': log2_baseline,
                'log2_ratio_current': log2_current,
                'log2_fold_change': log2_fold,
                'fold_change': _safe_fold(log2_fold),
            })
    return pairs


def _labels_permuted(units, labels):
    return [dict(unit, period=label) for unit, label in zip(units, labels)]


def _permute_labels_global(labels, rng):
    permuted = list(labels)
    rng.shuffle(permuted)
    return permuted


def _permute_labels_within_supplier(units, labels, rng):
    """仅在 supplier 内部打乱期间标签；批次单元与其行保持绑定。"""
    permuted = list(labels)
    supplier_indices = defaultdict(list)
    for index, unit in enumerate(units):
        supplier_indices[unit['supplier']].append(index)
    for indices in supplier_indices.values():
        subset = [labels[index] for index in indices]
        rng.shuffle(subset)
        for index, label in zip(indices, subset):
            permuted[index] = label
    return permuted


def _total_statistic(units, baseline, current):
    centroids = _period_centroids(units, (baseline, current))
    if baseline not in centroids or current not in centroids:
        return None
    return _norm([c - b for c, b in zip(centroids[current],
                                        centroids[baseline])])


def _within_statistic(units, baseline, current, common_suppliers):
    """共同供应方内部配比位移：供应方位移向量的加权平均的范数。

    权重取两期供应方权重份额的均值，与分解中的 within_supplier 项一致；
    方向一致的位移会叠加，比逐供应方距离的均方根更能识别整体配比变化。
    """
    dimension = len(units[0]['clr'])
    period_totals = {period: math.fsum(
        unit['weight'] for unit in units if unit['period'] == period)
        for period in (baseline, current)}
    shift = [0.0] * dimension
    distances = {}
    for supplier in common_suppliers:
        members = [unit for unit in units if unit['supplier'] == supplier]
        base = [unit for unit in members if unit['period'] == baseline]
        curr = [unit for unit in members if unit['period'] == current]
        if not base or not curr:
            continue
        centroid_b = _wmean([unit['clr'] for unit in base],
                            [unit['weight'] for unit in base])
        centroid_c = _wmean([unit['clr'] for unit in curr],
                            [unit['weight'] for unit in curr])
        distances[supplier] = _norm([c - b for c, b in zip(centroid_c,
                                                          centroid_b)])
        weight_b = math.fsum(unit['weight'] for unit in base)
        weight_c = math.fsum(unit['weight'] for unit in curr)
        mean_share = 0.5 * (weight_b / period_totals[baseline]
                            + weight_c / period_totals[current])
        for index in range(dimension):
            shift[index] += mean_share * (centroid_c[index]
                                          - centroid_b[index])
    if not distances:
        return None, distances
    return _norm(shift), distances


def _permute_total(units, config, baseline, current, seed_offset):
    observed = _total_statistic(units, baseline, current)
    if observed is None:
        return {'statistic': None, 'p_value': None,
                'reason': 'missing_period',
                'permutations': config.permutations,
                'valid_permutations': 0}
    rng = random.Random(config.seed + seed_offset)
    labels = [unit['period'] for unit in units]
    valid = 0
    extreme = 0
    for _ in range(config.permutations):
        value = _total_statistic(
            _labels_permuted(units, _permute_labels_global(labels, rng)),
            baseline, current)
        if value is None:
            continue
        valid += 1
        if value >= observed - _EPS:
            extreme += 1
    return {'statistic': observed,
            'p_value': (1 + extreme) / (1 + valid),
            'permutations': config.permutations,
            'valid_permutations': valid}


def _permute_within(units, config, baseline, current, common_suppliers,
                    seed_offset):
    empty = {'statistic': None, 'p_value': None,
             'reason': 'no_common_suppliers',
             'permutations': config.permutations,
             'valid_permutations': 0,
             'supplier_p_values': {}, 'supplier_distances': {}}
    if not common_suppliers:
        return empty
    observed, observed_distances = _within_statistic(
        units, baseline, current, common_suppliers)
    if observed is None:
        return empty
    rng = random.Random(config.seed + seed_offset)
    labels = [unit['period'] for unit in units]
    valid = 0
    extreme = 0
    supplier_extreme = defaultdict(int)
    for _ in range(config.permutations):
        permuted = _permute_labels_within_supplier(units, labels, rng)
        value, distances = _within_statistic(
            _labels_permuted(units, permuted), baseline, current,
            common_suppliers)
        if value is None:
            continue
        valid += 1
        if value >= observed - _EPS:
            extreme += 1
        for supplier, distance in distances.items():
            if distance >= observed_distances.get(supplier, 0.0) - _EPS:
                supplier_extreme[supplier] += 1
    supplier_p = {supplier: (1 + supplier_extreme[supplier]) / (1 + valid)
                  for supplier in common_suppliers}
    return {'statistic': observed,
            'p_value': (1 + extreme) / (1 + valid),
            'permutations': config.permutations,
            'valid_permutations': valid,
            'supplier_p_values': supplier_p,
            'supplier_distances': observed_distances}


def _test_block(result, alpha, name, confounded=False,
                statistic_name='aitchison_distance'):
    p_value = result['p_value']
    decision = None
    if p_value is not None:
        decision = 'shift_indicated' if p_value < alpha else 'no_clear_shift'
    return {
        'name': name,
        'statistic': {'name': statistic_name,
                      'value': result['statistic']},
        'p_value': p_value,
        'permutations': result['permutations'],
        'valid_permutations': result['valid_permutations'],
        'alpha': alpha,
        'decision': decision,
        'reason': result.get('reason'),
        'confounded_with_supplier_structure': confounded,
    }


def _scheme_metrics(units, weighting, config, baseline, current,
                    used_components, seed_base):
    dimension = len(units[0]['clr'])
    centroids = _period_centroids(units, (baseline, current))
    table = _supplier_table(units, (baseline, current))
    decomposition = _decompose(table, baseline, current, dimension)
    total_vector = [centroids[current][i] - centroids[baseline][i]
                    for i in range(dimension)]
    residual = [total_vector[i]
                - (decomposition['within_supplier']['clr'][i]
                   if decomposition['within_supplier'] else 0.0)
                - (decomposition['supplier_structure']['clr'][i]
                   if decomposition['supplier_structure'] else 0.0)
                - decomposition['non_separable']['clr'][i]
                for i in range(dimension)]

    common = decomposition['common_suppliers']
    total_test = _permute_total(units, config, baseline, current, seed_base)
    within_test = _permute_within(units, config, baseline, current, common,
                                  seed_base + 1)
    confounded = bool(decomposition['entered_suppliers']
                      or decomposition['exited_suppliers'])

    supplier_details = []
    suppliers = sorted({supplier for (supplier, _) in table})
    supplier_p = within_test.get('supplier_p_values', {})
    supplier_d = within_test.get('supplier_distances', {})
    for supplier in suppliers:
        entry_b = table.get((supplier, baseline))
        entry_c = table.get((supplier, current))
        if entry_b and entry_c:
            status = 'common'
            distance = supplier_d.get(supplier)
            p_value = supplier_p.get(supplier)
        elif entry_c:
            status, distance, p_value = 'entered', None, None
        else:
            status, distance, p_value = 'exited', None, None
        supplier_details.append({
            'supplier': supplier,
            'status': status,
            'weight_share_baseline': entry_b['weight_share'] if entry_b else None,
            'weight_share_current': entry_c['weight_share'] if entry_c else None,
            'batch_units_baseline': entry_b['batch_units'] if entry_b else 0,
            'batch_units_current': entry_c['batch_units'] if entry_c else 0,
            'distance': distance,
            'permutation_p_value': p_value,
        })

    period_centroids = {}
    for period in (baseline, current):
        shares = _geometric_shares(centroids[period])
        period_centroids[str(period)] = {
            'clr': list(centroids[period]),
            'geometric_mean_share': dict(zip(used_components, shares)),
        }

    return {
        'label': WEIGHTING_LABELS[weighting],
        'batch_units': len(units),
        'rows': sum(unit['rows'] for unit in units),
        'period_centroids': period_centroids,
        'supplier_details': supplier_details,
        'decomposition': decomposition,
        'total': {'clr': list(total_vector), 'shift': _norm(total_vector)},
        'decomposition_residual_norm': _norm(residual),
        'ratio_changes': _ratio_changes(centroids[baseline],
                                        centroids[current], used_components),
        'tests': {
            'total': _test_block(total_test, config.alpha, '两期总体分布检验',
                                 confounded=confounded),
            'within_supplier': _test_block(
                within_test, config.alpha, '供应方内部配比检验',
                statistic_name='weighted_within_supplier_shift'),
        },
    }


def _run_schemes(prepared, config, baseline, current, used_components,
                 seed_base):
    schemes = {}
    for index, weighting in enumerate(WEIGHTINGS):
        units = _build_units(prepared, weighting)
        if not units:
            continue
        schemes[weighting] = _scheme_metrics(
            units, weighting, config, baseline, current, used_components,
            seed_base + 10 * index)
    return schemes


def _sensitivity(screened, config, baseline, current):
    if config.zero_policy != 'detection_limit':
        return {'applicable': False,
                'reason': 'zero_policy_not_detection_limit',
                'fractions': [], 'entries': [], 'summary': {}}
    if not any(item['zeros'] for item in screened):
        return {'applicable': False, 'reason': 'no_zero_values',
                'fractions': [], 'entries': [], 'summary': {}}

    fractions = sorted(set(config.sensitivity_fractions)
                       | {config.zero_fraction})
    entries = []
    for step, fraction in enumerate(fractions):
        prepared_info, failure = _prepare(screened, config, fraction)
        entry = {'zero_fraction': fraction,
                 'primary': fraction == config.zero_fraction}
        if prepared_info is None:
            entry['status'] = 'unavailable'
            entry['reason'] = failure['reason']
            entries.append(entry)
            continue
        selected = [item for item in prepared_info['prepared']
                    if item['row']['period'] in (baseline, current)]
        entry['status'] = 'ok'
        entry['rows_used'] = len(selected)
        entry['imputed_zeros'] = prepared_info['imputed_zeros']
        schemes = _run_schemes(selected, config, baseline, current,
                               prepared_info['used_components'],
                               1000 + 100 * step)
        for weighting, scheme in schemes.items():
            decomposition = scheme['decomposition']
            entry[weighting] = {
                'total_distance': scheme['total']['shift'],
                'within_supplier_shift': (
                    decomposition['within_supplier']['shift']
                    if decomposition['within_supplier'] else None),
                'supplier_structure_shift': (
                    decomposition['supplier_structure']['shift']
                    if decomposition['supplier_structure'] else None),
                'p_total': scheme['tests']['total']['p_value'],
                'p_within_supplier': (
                    scheme['tests']['within_supplier']['p_value']),
            }
        entries.append(entry)

    summary = {}
    for weighting in WEIGHTINGS:
        distances = [entry[weighting]['total_distance'] for entry in entries
                     if entry.get('status') == 'ok' and weighting in entry]
        p_total = [entry[weighting]['p_total'] for entry in entries
                   if entry.get('status') == 'ok' and weighting in entry
                   and entry[weighting]['p_total'] is not None]
        p_within = [entry[weighting]['p_within_supplier'] for entry in entries
                    if entry.get('status') == 'ok' and weighting in entry
                    and entry[weighting]['p_within_supplier'] is not None]

        def stability(p_values):
            if not p_values:
                return None
            below = all(p < config.alpha for p in p_values)
            above = all(p >= config.alpha for p in p_values)
            return below or above

        summary[weighting] = {
            'total_distance_min': min(distances) if distances else None,
            'total_distance_max': max(distances) if distances else None,
            'p_total_min': min(p_total) if p_total else None,
            'p_total_max': max(p_total) if p_total else None,
            'p_within_supplier_min': min(p_within) if p_within else None,
            'p_within_supplier_max': max(p_within) if p_within else None,
            'decision_stable_total': stability(p_total),
            'decision_stable_within_supplier': stability(p_within),
        }
    return {'applicable': True, 'fractions': fractions,
            'primary_fraction': config.zero_fraction,
            'entries': entries, 'summary': summary}


def _unavailable(reason, reason_text, ordered, config, validation=None,
                   composition=None):
    return {
        'product': PRODUCT,
        'analysis': 'compositional_comparison',
        'schema_version': 1,
        'unit': UNIT,
        'rows': len(ordered),
        'input_fingerprint': fingerprint(ordered),
        'status': 'unavailable',
        'reason': reason,
        'reason_text': reason_text,
        'config': _config_block(config),
        'periods': {'baseline': config.baseline_period,
                    'current': config.current_period,
                    'observed': sorted({row['period'] for row in ordered})},
        'validation': validation,
        'composition': composition,
        'by_weighting': {},
        'zero_sensitivity': {'applicable': False, 'reason': reason,
                             'fractions': [], 'entries': [], 'summary': {}},
        'notes': _notes(),
    }


def _config_block(config):
    return {
        'closure_tolerance': config.closure_tolerance,
        'zero_policy': config.zero_policy,
        'zero_fraction': config.zero_fraction,
        'sensitivity_fractions': list(config.sensitivity_fractions),
        'permutations': config.permutations,
        'seed': config.seed,
        'alpha': config.alpha,
        'baseline_period': config.baseline_period,
        'current_period': config.current_period,
        'weighting_schemes': [
            {'key': weighting, 'label': WEIGHTING_LABELS[weighting]}
            for weighting in WEIGHTINGS],
    }


def _notes():
    return [
        '距离为 CLR 表示下的 Aitchison 距离；CLR 坐标正负只表示相对几何均值'
        '的高低，不能直接解释为某种材料绝对增加。',
        '组分份额受闭合约束，一项上升必然牵动其他项；解释以组分比变化为准。',
        '每批等权与 lot_size 物质量加权分别计算，未合并为同一统计总体。',
        '置换检验以批次为单元重采样，保留 supplier 内批次结构。',
        '零值成因（检测限以下或结构性不存在）无法从数值本身区分，'
        '由 zero_policy 显式声明；detection_limit 策略按声明比例替换并做'
        '敏感性比较，structural 策略将零组分剔除后分析子组成。',
        'null 表示缺少共同供应方或数据不足，不是零。',
        '本结果为质量监测线索，不是不合格判定。',
    ]


def analyze(records, config=None):
    config = config or Config()
    check_config(config)
    ordered = sorted(records, key=lambda row: row['observation_id'])

    if not ordered:
        return _unavailable('no_rows', '没有观测记录', ordered, config)

    screened, validation = _screen(ordered, config)
    if not screened:
        return _unavailable('no_usable_rows', '没有通过验证的观测记录',
                            ordered, config, validation=validation)

    observed_periods = sorted({item['row']['period'] for item in screened})
    if config.baseline_period is None:
        if len(observed_periods) != 2:
            return _unavailable(
                'period_selection_required',
                '比较期不唯一，需通过 --baseline/--current 指定两个比较期',
                ordered, config, validation=validation)
        baseline, current = observed_periods
    else:
        baseline, current = config.baseline_period, config.current_period
        if baseline not in observed_periods or current not in observed_periods:
            return _unavailable(
                'missing_period', '指定的比较期在有效数据中不存在',
                ordered, config, validation=validation)

    prepared_info, failure = _prepare(screened, config, config.zero_fraction)
    if prepared_info is None:
        return _unavailable(failure['reason'],
                            '零值策略下可分析子组成不足两个组分',
                            ordered, config, validation=validation)

    selected = [item for item in prepared_info['prepared']
                if item['row']['period'] in (baseline, current)]
    outside = len(prepared_info['prepared']) - len(selected)
    if not selected:
        return _unavailable('missing_period', '指定比较期内没有可用观测',
                            ordered, config, validation=validation)

    for reason, ids in prepared_info['excluded'].items():
        validation['excluded_by_reason'][reason] = {
            'count': len(ids), 'label': REASON_LABELS[reason],
            'examples': ids[:5]}
    validation['rows_used'] = len(prepared_info['prepared'])
    validation['rows_in_comparison'] = len(selected)
    validation['rows_outside_comparison'] = outside
    validation['rows_excluded'] = (validation['rows_total']
                                   - validation['rows_used'])

    composition = {
        'components': list(COMPONENTS),
        'used_components': list(prepared_info['used_components']),
        'excluded_components': list(prepared_info['excluded_components']),
        'zero_policy': config.zero_policy,
        'zero_fraction': prepared_info['zero_fraction'],
        'imputed_zeros': prepared_info['imputed_zeros'],
        'zero_interpretation': (
            '零值按检测限以下处理，替换为 zero_fraction × detection_limit '
            '并做乘法替换保持闭合'
            if config.zero_policy == 'detection_limit'
            else '零值按结构性不存在处理，对应组分从子组成中剔除'),
    }

    schemes = _run_schemes(selected, config, baseline, current,
                           prepared_info['used_components'], 0)
    if not schemes:
        return _unavailable('no_usable_rows', '比较期内没有可用批次',
                            ordered, config, validation=validation,
                            composition=composition)

    return {
        'product': PRODUCT,
        'analysis': 'compositional_comparison',
        'schema_version': 1,
        'unit': UNIT,
        'rows': len(ordered),
        'input_fingerprint': fingerprint(ordered),
        'status': 'ok',
        'reason': None,
        'reason_text': None,
        'config': _config_block(config),
        'periods': {'baseline': baseline, 'current': current,
                    'observed': observed_periods},
        'validation': validation,
        'composition': composition,
        'by_weighting': schemes,
        'zero_sensitivity': _sensitivity(screened, config, baseline, current),
        'notes': _notes(),
    }
