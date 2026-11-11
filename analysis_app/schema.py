FIELDS = {'observation_id': 'str', 'batch': 'str', 'supplier': 'str', 'period': 'int', 'component_a': 'float', 'component_b': 'float', 'component_c': 'float', 'lot_size': 'float', 'detection_limit': 'float'}
REQUIRED = ['observation_id', 'batch', 'supplier', 'component_a']
ENTITY = 'batch'
GROUP = 'supplier'
MEASURE = 'component_a'
PRODUCT = 'compositional-shift'
UNIT = '质量份额'
