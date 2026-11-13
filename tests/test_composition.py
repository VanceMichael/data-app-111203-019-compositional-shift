import copy
import json
import math
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analysis_app.composition import (Config, _build_units, _clr, _permute_labels_within_supplier,
                                      _prepare, _screen, analyze)
from analysis_app.io import load_records
from analysis_app.schema import COMPONENTS


def make_row(obs, batch, supplier, period, a, b, c, lot=10.0, dl=0.001):
    return {'observation_id': obs, 'batch': batch, 'supplier': supplier,
            'period': period, 'component_a': a, 'component_b': b,
            'component_c': c, 'lot_size': lot, 'detection_limit': dl}


def make_panel(composition_by_period, suppliers=('S0', 'S1'), batches=4, lot=10.0):
    rows = []
    for period, composition in composition_by_period.items():
        for supplier in suppliers:
            for index in range(batches):
                rows.append(make_row(
                    f'obs-{period}-{supplier}-{index}',
                    f'B-{period}-{supplier}-{index}', supplier, period,
                    *composition, lot=lot))
    return rows


FAST = {'permutations': 99, 'seed': 7}


class ConfigTest(unittest.TestCase):
    def test_invalid_zero_fraction_rejected(self):
        with self.assertRaisesRegex(ValueError, '零值替换比例'):
            analyze([], Config(zero_fraction=0.0))

    def test_invalid_policy_rejected(self):
        with self.assertRaisesRegex(ValueError, '未知零值策略'):
            analyze([], Config(zero_policy='add_constant'))

    def test_negative_closure_tolerance_rejected(self):
        with self.assertRaisesRegex(ValueError, '闭合容差'):
            analyze([], Config(closure_tolerance=-1.0))

    def test_same_periods_rejected(self):
        with self.assertRaisesRegex(ValueError, '不能相同'):
            analyze([], Config(baseline_period=0, current_period=0))

    def test_single_period_selection_rejected(self):
        with self.assertRaisesRegex(ValueError, '同时给出'):
            analyze([], Config(baseline_period=0))


class ValidationTest(unittest.TestCase):
    def test_all_zero_batch_excluded(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.25, 0.3, 0.45)})
        rows.append(make_row('obs-zero', 'B-zero', 'S0', 0, 0.0, 0.0, 0.0))
        result = analyze(rows, Config(**FAST))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['validation']['all_zero_rows'], 1)
        self.assertEqual(
            result['validation']['excluded_by_reason']['zero_total']['count'], 1)

    def test_all_zero_dataset_unavailable(self):
        rows = [make_row('obs-z0', 'B-z0', 'S0', 0, 0.0, 0.0, 0.0),
                make_row('obs-z1', 'B-z1', 'S0', 1, 0.0, 0.0, 0.0)]
        result = analyze(rows, Config(**FAST))
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'no_usable_rows')

    def test_negative_component_excluded(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.25, 0.3, 0.45)})
        rows.append(make_row('obs-neg', 'B-neg', 'S0', 0, -0.1, 0.6, 0.5))
        result = analyze(rows, Config(**FAST))
        self.assertEqual(result['validation']['nonnegative_violations'], 1)
        self.assertIn('negative_component',
                      result['validation']['excluded_by_reason'])

    def test_closure_tolerance_enforced(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.25, 0.3, 0.45)})
        rows.append(make_row('obs-open', 'B-open', 'S0', 0, 0.2, 0.3, 0.6))
        result = analyze(rows, Config(**FAST))
        self.assertEqual(
            result['validation']['excluded_by_reason']['closure_tolerance']['count'], 1)
        self.assertAlmostEqual(result['validation']['max_closure_error'], 0.1)

    def test_closure_within_tolerance_renormalized(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.2, 0.3, 0.5)})
        rows.append(make_row('obs-eps', 'B-eps', 'S0', 0,
                             0.2, 0.3, 0.5 + 5e-7))
        result = analyze(rows, Config(**FAST))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['validation']['rows_excluded'], 0)
        self.assertGreater(result['validation']['max_closure_error_used'], 0)

    def test_invalid_detection_limit_excluded(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.25, 0.3, 0.45)})
        rows.append(make_row('obs-dl', 'B-dl', 'S0', 0, 0.2, 0.3, 0.5, dl=0.0))
        result = analyze(rows, Config(**FAST))
        self.assertIn('invalid_detection_limit',
                      result['validation']['excluded_by_reason'])

    def test_batch_spanning_periods_excluded(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.25, 0.3, 0.45)})
        rows.append(make_row('obs-x0', 'B-shared', 'S0', 0, 0.2, 0.3, 0.5))
        rows.append(make_row('obs-x1', 'B-shared', 'S0', 1, 0.2, 0.3, 0.5))
        result = analyze(rows, Config(**FAST))
        excluded = result['validation']['excluded_by_reason']
        self.assertEqual(
            excluded['batch_spans_supplier_or_period']['count'], 2)
        self.assertEqual(result['validation']['invalid_batches'], ['B-shared'])

    def test_positive_below_detection_limit_counted(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.2, 0.3, 0.5)})
        rows.append(make_row('obs-low', 'B-low', 'S0', 0,
                             0.0005, 0.3, 0.6995, dl=0.001))
        result = analyze(rows, Config(**FAST))
        counts = result['validation']['positive_below_detection_limit']
        self.assertEqual(counts['component_a'], 1)
        self.assertEqual(result['validation']['zero_counts']['component_a'], 0)


class ZeroPolicyTest(unittest.TestCase):
    def rows_with_structural_zero(self):
        rows = []
        for period, (a, b) in ((0, (0.4, 0.6)), (1, (0.5, 0.5))):
            for index in range(4):
                rows.append(make_row(f'obs-{period}-{index}',
                                     f'B-{period}-{index}', 'S0', period,
                                     a, b, 0.0))
        return rows

    def test_structural_zero_drops_component(self):
        result = analyze(self.rows_with_structural_zero(),
                         Config(zero_policy='structural', **FAST))
        self.assertEqual(result['status'], 'ok')
        composition = result['composition']
        self.assertEqual(composition['used_components'],
                         ['component_a', 'component_b'])
        self.assertEqual(composition['excluded_components'], ['component_c'])
        self.assertEqual(composition['imputed_zeros'], 0)
        ratios = result['by_weighting']['equal_batch']['ratio_changes']
        self.assertEqual(len(ratios), 1)
        self.assertAlmostEqual(ratios[0]['log2_fold_change'],
                               math.log2((0.5 / 0.5) / (0.4 / 0.6)), places=9)

    def test_structural_zero_not_imputed_with_constant(self):
        result = analyze(self.rows_with_structural_zero(),
                         Config(zero_policy='structural', **FAST))
        self.assertIsNone(result['composition']['zero_fraction'])
        self.assertFalse(result['zero_sensitivity']['applicable'])
        self.assertEqual(result['zero_sensitivity']['reason'],
                         'zero_policy_not_detection_limit')

    def test_detection_limit_policy_imputes(self):
        result = analyze(self.rows_with_structural_zero(),
                         Config(zero_policy='detection_limit',
                                zero_fraction=0.5, **FAST))
        composition = result['composition']
        self.assertEqual(composition['used_components'], list(COMPONENTS))
        self.assertEqual(composition['imputed_zeros'], 8)
        self.assertEqual(composition['zero_fraction'], 0.5)

    def test_reject_policy_raises_on_zeros(self):
        with self.assertRaisesRegex(ValueError, 'reject'):
            analyze(self.rows_with_structural_zero(),
                    Config(zero_policy='reject', **FAST))

    def test_imputation_preserves_closure_and_positive_ratios(self):
        rows = [make_row('obs-0', 'B-0', 'S0', 0, 0.2, 0.8, 0.0, dl=0.01)]
        config = Config(zero_fraction=0.5)
        screened, _ = _screen(rows, config)
        prepared, failure = _prepare(screened, config, 0.5)
        self.assertIsNone(failure)
        values = prepared['prepared'][0]['values']
        self.assertAlmostEqual(math.fsum(values), 1.0)
        self.assertAlmostEqual(values[0] / values[1], 0.2 / 0.8)
        self.assertAlmostEqual(values[2], 0.005)

    def test_zero_replacement_impossible_excluded(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.2, 0.3, 0.5)})
        rows.append(make_row('obs-big', 'B-big', 'S0', 0, 1.0, 0.0, 0.0,
                             dl=0.6))
        result = analyze(rows, Config(zero_fraction=1.0, **FAST))
        self.assertIn('zero_replacement_impossible',
                      result['validation']['excluded_by_reason'])

    def test_zero_sensitivity_over_fraction_range(self):
        rows = []
        for period, (a, b) in ((0, (0.4, 0.6)), (1, (0.45, 0.55))):
            for index in range(4):
                rows.append(make_row(f'obs-{period}-{index}',
                                     f'B-{period}-{index}', 'S0', period,
                                     a, b, 0.0, dl=0.001))
        result = analyze(rows, Config(
            zero_fraction=0.5, sensitivity_fractions=(0.25, 0.5, 1.0), **FAST))
        sensitivity = result['zero_sensitivity']
        self.assertTrue(sensitivity['applicable'])
        self.assertEqual(sensitivity['fractions'], [0.25, 0.5, 1.0])
        self.assertEqual(len(sensitivity['entries']), 3)
        primary = [e for e in sensitivity['entries'] if e['primary']]
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary[0]['zero_fraction'], 0.5)
        summary = sensitivity['summary']['equal_batch']
        self.assertLessEqual(summary['total_distance_min'],
                             summary['total_distance_max'])
        self.assertIsNotNone(summary['decision_stable_total'])

    def test_no_zeros_sensitivity_not_applicable(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.25, 0.3, 0.45)})
        result = analyze(rows, Config(**FAST))
        self.assertFalse(result['zero_sensitivity']['applicable'])
        self.assertEqual(result['zero_sensitivity']['reason'], 'no_zero_values')


class GeometryTest(unittest.TestCase):
    def test_proportional_common_scaling_is_no_shift(self):
        rows = []
        for index in range(4):
            rows.append(make_row(f'obs-0-{index}', f'B-0-{index}', 'S0', 0,
                                 0.2, 0.3, 0.5))
            rows.append(make_row(f'obs-1-{index}', f'B-1-{index}', 'S0', 1,
                                 0.4, 0.6, 1.0))
        result = analyze(rows, Config(closure_tolerance=1.5, **FAST))
        self.assertEqual(result['status'], 'ok')
        scheme = result['by_weighting']['equal_batch']
        self.assertAlmostEqual(scheme['total']['shift'], 0.0, places=12)
        for pair in scheme['ratio_changes']:
            self.assertAlmostEqual(pair['log2_fold_change'], 0.0, places=9)
        self.assertEqual(scheme['tests']['total']['p_value'], 1.0)

    def test_tiny_positive_values_stay_finite(self):
        tiny0, tiny1 = 1e-12, 2e-12
        rows = []
        for index in range(4):
            rows.append(make_row(f'obs-0-{index}', f'B-0-{index}', 'S0', 0,
                                 tiny0, 0.3, 0.7))
            rows.append(make_row(f'obs-1-{index}', f'B-1-{index}', 'S0', 1,
                                 tiny1, 0.3, 0.7))
        result = analyze(rows, Config(**FAST))
        self.assertEqual(result['status'], 'ok')
        scheme = result['by_weighting']['equal_batch']
        self.assertTrue(math.isfinite(scheme['total']['shift']))
        self.assertGreater(scheme['total']['shift'], 0)
        pair_ab = [p for p in scheme['ratio_changes']
                   if p['pair'] == 'component_a/component_b'][0]
        self.assertAlmostEqual(pair_ab['log2_fold_change'], 1.0, places=6)
        for value in json.dumps(scheme, allow_nan=False) and [scheme]:
            pass

    def test_ratio_changes_interpretable(self):
        rows = make_panel({0: (0.2, 0.4, 0.4), 1: (0.4, 0.2, 0.4)},
                          suppliers=('S0',))
        result = analyze(rows, Config(**FAST))
        ratios = {p['pair']: p for p in
                  result['by_weighting']['equal_batch']['ratio_changes']}
        self.assertAlmostEqual(
            ratios['component_a/component_b']['log2_fold_change'], 2.0)
        self.assertAlmostEqual(
            ratios['component_a/component_b']['fold_change'], 4.0)
        self.assertAlmostEqual(
            ratios['component_a/component_c']['log2_fold_change'], 1.0)
        self.assertAlmostEqual(
            ratios['component_b/component_c']['log2_fold_change'], -1.0)

    def test_notes_forbid_coordinate_material_reading(self):
        result = analyze(make_panel({0: (0.2, 0.3, 0.5), 1: (0.2, 0.3, 0.5)}),
                         Config(**FAST))
        joined = ''.join(result['notes'])
        self.assertIn('不能直接解释为某种材料绝对增加', joined)
        self.assertIn('不是不合格判定', joined)


class DecompositionTest(unittest.TestCase):
    def test_complete_supplier_replacement_not_separable(self):
        rows = []
        for period, suppliers in ((0, ('S0', 'S1')), (1, ('S2', 'S3'))):
            composition = (0.2, 0.3, 0.5) if period == 0 else (0.3, 0.3, 0.4)
            for supplier in suppliers:
                for index in range(3):
                    rows.append(make_row(f'obs-{period}-{supplier}-{index}',
                                         f'B-{period}-{supplier}-{index}',
                                         supplier, period, *composition))
        result = analyze(rows, Config(**FAST))
        self.assertEqual(result['status'], 'ok')
        decomposition = result['by_weighting']['equal_batch']['decomposition']
        self.assertFalse(decomposition['separable'])
        self.assertIsNone(decomposition['within_supplier'])
        self.assertIsNone(decomposition['supplier_structure'])
        self.assertEqual(decomposition['common_suppliers'], [])
        self.assertEqual(sorted(decomposition['entered_suppliers']), ['S2', 'S3'])
        self.assertEqual(sorted(decomposition['exited_suppliers']), ['S0', 'S1'])
        total = result['by_weighting']['equal_batch']['total']['shift']
        self.assertAlmostEqual(decomposition['non_separable']['shift'], total)
        self.assertIsNotNone(decomposition['non_separable_note'])
        within_test = result['by_weighting']['equal_batch']['tests']['within_supplier']
        self.assertIsNone(within_test['p_value'])
        self.assertEqual(within_test['reason'], 'no_common_suppliers')
        total_test = result['by_weighting']['equal_batch']['tests']['total']
        self.assertTrue(total_test['confounded_with_supplier_structure'])

    def test_partial_replacement_marks_non_separable_part(self):
        rows = []
        plan = {0: {'S0': (0.2, 0.3, 0.5), 'S1': (0.2, 0.3, 0.5)},
                1: {'S0': (0.2, 0.3, 0.5), 'S2': (0.6, 0.2, 0.2)}}
        for period, suppliers in plan.items():
            for supplier, composition in suppliers.items():
                for index in range(3):
                    rows.append(make_row(f'obs-{period}-{supplier}-{index}',
                                         f'B-{period}-{supplier}-{index}',
                                         supplier, period, *composition))
        result = analyze(rows, Config(**FAST))
        decomposition = result['by_weighting']['equal_batch']['decomposition']
        self.assertTrue(decomposition['separable'])
        self.assertEqual(decomposition['entered_suppliers'], ['S2'])
        self.assertEqual(decomposition['exited_suppliers'], ['S1'])
        self.assertGreater(decomposition['non_separable']['shift'], 0)
        self.assertAlmostEqual(
            decomposition['common_weight_share_baseline'], 0.5)
        residual = result['by_weighting']['equal_batch']['decomposition_residual_norm']
        self.assertLess(residual, 1e-9)

    def test_structure_only_change(self):
        rows = []
        plan = {0: {'S0': 3, 'S1': 1}, 1: {'S0': 1, 'S1': 3}}
        compositions = {'S0': (0.2, 0.3, 0.5), 'S1': (0.5, 0.3, 0.2)}
        for period, counts in plan.items():
            for supplier, count in counts.items():
                for index in range(count):
                    rows.append(make_row(f'obs-{period}-{supplier}-{index}',
                                         f'B-{period}-{supplier}-{index}',
                                         supplier, period,
                                         *compositions[supplier]))
        result = analyze(rows, Config(**FAST))
        decomposition = result['by_weighting']['equal_batch']['decomposition']
        self.assertAlmostEqual(
            decomposition['within_supplier']['shift'], 0.0, places=12)
        self.assertGreater(decomposition['supplier_structure']['shift'], 0)
        total = result['by_weighting']['equal_batch']['total']['shift']
        self.assertAlmostEqual(decomposition['supplier_structure']['shift'],
                               total, places=12)

    def test_within_only_change(self):
        rows = []
        plan = {0: (0.2, 0.3, 0.5), 1: (0.3, 0.3, 0.4)}
        for period, composition in plan.items():
            for supplier in ('S0', 'S1'):
                for index in range(3):
                    rows.append(make_row(f'obs-{period}-{supplier}-{index}',
                                         f'B-{period}-{supplier}-{index}',
                                         supplier, period, *composition))
        result = analyze(rows, Config(**FAST))
        decomposition = result['by_weighting']['equal_batch']['decomposition']
        self.assertAlmostEqual(
            decomposition['supplier_structure']['shift'], 0.0, places=12)
        self.assertGreater(decomposition['within_supplier']['shift'], 0)
        total = result['by_weighting']['equal_batch']['total']['shift']
        self.assertAlmostEqual(decomposition['within_supplier']['shift'],
                               total, places=12)


class WeightingTest(unittest.TestCase):
    def rows_unequal_lots(self):
        rows = []
        lots = {'S0': 10.0, 'S1': 30.0}
        compositions = {'S0': (0.2, 0.3, 0.5), 'S1': (0.5, 0.3, 0.2)}
        for period in (0, 1):
            for supplier in ('S0', 'S1'):
                rows.append(make_row(f'obs-{period}-{supplier}',
                                     f'B-{period}-{supplier}', supplier,
                                     period, *compositions[supplier],
                                     lot=lots[supplier]))
        return rows

    def test_weighting_schemes_are_separate_populations(self):
        result = analyze(self.rows_unequal_lots(), Config(**FAST))
        schemes = result['by_weighting']
        self.assertEqual(set(schemes), {'equal_batch', 'mass'})
        equal_weights = {d['supplier']: d['weight_share_baseline']
                         for d in schemes['equal_batch']['supplier_details']}
        mass_weights = {d['supplier']: d['weight_share_baseline']
                        for d in schemes['mass']['supplier_details']}
        self.assertEqual(equal_weights, {'S0': 0.5, 'S1': 0.5})
        self.assertEqual(mass_weights, {'S0': 0.25, 'S1': 0.75})
        self.assertNotEqual(schemes['equal_batch']['label'],
                            schemes['mass']['label'])

    def test_mass_centroid_uses_lot_weights(self):
        rows = [make_row('obs-0', 'B-0', 'S0', 0, 0.2, 0.3, 0.5, lot=10.0),
                make_row('obs-1', 'B-1', 'S0', 0, 0.5, 0.3, 0.2, lot=30.0),
                make_row('obs-2', 'B-2', 'S0', 1, 0.2, 0.3, 0.5, lot=10.0),
                make_row('obs-3', 'B-3', 'S0', 1, 0.5, 0.3, 0.2, lot=30.0)]
        result = analyze(rows, Config(**FAST))
        expected = tuple((10 * _clr((0.2, 0.3, 0.5))[i]
                          + 30 * _clr((0.5, 0.3, 0.2))[i]) / 40
                         for i in range(3))
        centroid = result['by_weighting']['mass']['period_centroids']['0']['clr']
        for got, want in zip(centroid, expected):
            self.assertAlmostEqual(got, want, places=12)

    def test_batch_units_aggregate_rows(self):
        rows = []
        for period in (0, 1):
            for index in range(3):
                rows.append(make_row(f'obs-{period}-{index}', 'B-shared-' + str(period),
                                     'S0', period, 0.2, 0.3, 0.5,
                                     lot=5.0))
        config = Config(**FAST)
        screened, _ = _screen(rows, config)
        prepared, _ = _prepare(screened, config, 0.5)
        equal_units = _build_units(prepared['prepared'], 'equal_batch')
        mass_units = _build_units(prepared['prepared'], 'mass')
        self.assertEqual(len(equal_units), 2)
        self.assertEqual([unit['rows'] for unit in equal_units], [3, 3])
        self.assertEqual([unit['weight'] for unit in equal_units], [1.0, 1.0])
        self.assertEqual([unit['weight'] for unit in mass_units], [15.0, 15.0])

    def test_within_permutation_keeps_supplier_structure(self):
        units = [{'batch': f'B-{supplier}-{period}-{index}',
                  'supplier': supplier,
                  'period': period, 'clr': (0.0, 0.0, 0.0), 'weight': 1.0,
                  'rows': 2}
                 for supplier in ('S0', 'S1')
                 for period in (0, 1)
                 for index in range(3)]
        labels = [unit['period'] for unit in units]
        rng = random.Random(3)
        for _ in range(20):
            permuted = _permute_labels_within_supplier(units, labels, rng)
            self.assertEqual(sorted(permuted), sorted(labels))
            for supplier in ('S0', 'S1'):
                original = sorted(label for unit, label in zip(units, labels)
                                  if unit['supplier'] == supplier)
                shuffled = sorted(label for unit, label in zip(units, permuted)
                                  if unit['supplier'] == supplier)
                self.assertEqual(original, shuffled)
        self.assertEqual(len({unit['batch'] for unit in units}), 12)


class RobustnessTest(unittest.TestCase):
    def test_determinism_and_order_invariance(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.25, 0.3, 0.45)})
        config = Config(**FAST)
        first = analyze(rows, config)
        shuffled = copy.deepcopy(rows)
        random.Random(11).shuffle(shuffled)
        second = analyze(shuffled, config)
        self.assertEqual(first, second)

    def test_empty_dataset(self):
        result = analyze([], Config(**FAST))
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'no_rows')
        self.assertEqual(result['by_weighting'], {})

    def test_single_period_unavailable(self):
        result = analyze(make_panel({0: (0.2, 0.3, 0.5)}), Config(**FAST))
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'period_selection_required')

    def test_three_periods_require_selection(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.25, 0.3, 0.45),
                           2: (0.3, 0.3, 0.4)})
        result = analyze(rows, Config(**FAST))
        self.assertEqual(result['reason'], 'period_selection_required')
        selected = analyze(rows, Config(baseline_period=0, current_period=2,
                                        **FAST))
        self.assertEqual(selected['status'], 'ok')
        self.assertEqual(selected['periods']['baseline'], 0)
        self.assertEqual(selected['periods']['current'], 2)
        self.assertGreater(selected['validation']['rows_outside_comparison'], 0)

    def test_missing_requested_period_unavailable(self):
        rows = make_panel({0: (0.2, 0.3, 0.5), 1: (0.25, 0.3, 0.45)})
        result = analyze(rows, Config(baseline_period=0, current_period=5,
                                      **FAST))
        self.assertEqual(result['reason'], 'missing_period')

    def test_fixture_analysis_signals_within_supplier_shift(self):
        records = load_records('data/observations.csv')
        result = analyze(records, Config(permutations=999, seed=20260101))
        self.assertEqual(result['status'], 'ok')
        for weighting in ('equal_batch', 'mass'):
            scheme = result['by_weighting'][weighting]
            decomposition = scheme['decomposition']
            self.assertTrue(decomposition['separable'])
            self.assertEqual(decomposition['entered_suppliers'], [])
            self.assertEqual(decomposition['exited_suppliers'], [])
            self.assertAlmostEqual(
                decomposition['supplier_structure']['shift'], 0.0, places=9)
            self.assertGreater(decomposition['within_supplier']['shift'], 0)
            self.assertLess(scheme['decomposition_residual_norm'], 1e-9)
            self.assertEqual(scheme['tests']['within_supplier']['decision'],
                             'shift_indicated')


class CliTest(unittest.TestCase):
    def test_cli_composition_matches_library(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            run = subprocess.run(
                [sys.executable, '-m', 'analysis_app', 'composition',
                 '--permutations', '99', '--output', str(path)],
                text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            from analysis_app.composition import analyze as lib_analyze
            expected = lib_analyze(load_records('data/observations.csv'),
                                   Config(permutations=99))
            self.assertEqual(json.loads(run.stdout), expected)
            self.assertEqual(json.loads(path.read_text()), expected)

    def test_cli_reject_policy_fails_on_zero_data(self):
        rows = [make_row('obs-0', 'B-0', 'S0', 0, 0.5, 0.5, 0.0),
                make_row('obs-1', 'B-1', 'S0', 1, 0.5, 0.5, 0.0)]
        with tempfile.TemporaryDirectory() as directory:
            import csv
            path = Path(directory) / 'zeros.csv'
            with path.open('w', encoding='utf-8', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            run = subprocess.run(
                [sys.executable, '-m', 'analysis_app', 'composition',
                 '--input', str(path), '--zero-policy', 'reject'],
                capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertIn('reject', run.stderr)

    def test_cli_invalid_fraction_fails(self):
        run = subprocess.run(
            [sys.executable, '-m', 'analysis_app', 'composition',
             '--zero-fraction', '0'],
            capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)


if __name__ == '__main__':
    unittest.main()
