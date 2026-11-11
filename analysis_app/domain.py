import math
import statistics
from collections import Counter


def describe(records):
    return {'material_total': math.fsum(row['lot_size'] for row in records), 'maximum_closure_error': max(abs(row['component_a'] + row['component_b'] + row['component_c'] - 1) for row in records)}
