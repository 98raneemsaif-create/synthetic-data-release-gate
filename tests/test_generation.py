"""Generation contract and local demo; independent of optional SDV installation."""
import json
import math
import sys
import types
import pandas as pd
import pytest
from gate.reference import make_reference
from gate.generate import generate, export, GenerationError
from gate.checks.hard import run_hard_checks
from gate.models import Status
from gate.canonical import canonical_content_hash


def test_reference_is_reproducible_and_integral():
    a=make_reference(80,29); b=make_reference(80,29)
    assert set(a)=={'customers','orders','order_items'}
    for k in a: pd.testing.assert_frame_equal(a[k],b[k])
    assert all(c.status is Status.PASS for c in run_hard_checks(a))
    assert len(a['orders'])>=len(a['customers'])


def test_demo_generates_new_linked_keys():
    source=make_reference(40,10)
    result=generate(source,backend='demo',seed=10,scale=.75)
    assert result.backend=='demo-relational-bootstrap'
    assert len(result.tables['customers'])==30
    assert all(c.status is Status.PASS for c in run_hard_checks(result.tables))
    assert set(result.tables['customers'].customer_id).isdisjoint(set(source['customers'].customer_id))
    assert result.metadata['warning'].startswith('NOT SDV')


def test_demo_deterministic():
    source=make_reference(45)
    x=generate(source,backend='demo',seed=11)
    y=generate(source,backend='demo',seed=11)
    for k in x.tables: pd.testing.assert_frame_equal(x.tables[k],y.tables[k])


def test_invalid_references_refused():
    source=make_reference(20)
    source['orders'].loc[0,'customer_id']='MISSING'
    with pytest.raises(GenerationError,match='failed structural validation'):
        generate(source,backend='demo')


def test_invalid_source_schema_refused():
    source = make_reference(20)
    source['order_items'] = source['order_items'].drop(columns=['price'])
    with pytest.raises(GenerationError, match='missing required columns'):
        generate(source, backend='demo')

@pytest.mark.parametrize('scale',[-1,0,float('nan'),float('inf'),True])
def test_bad_scale_fails_closed(scale):
    with pytest.raises(GenerationError,match='Invalid seed or scale'):
        generate(make_reference(20),backend='demo',scale=scale)


def test_bad_backend_never_falls_back():
    with pytest.raises(GenerationError,match='backend must'):
        generate(make_reference(20),backend='other')


def test_export_private_manifest_and_no_overwrite(tmp_path):
    result=generate(make_reference(20),backend='demo')
    target=tmp_path/'candidate'
    export(result,target)
    manifest=json.loads((target/'generation_manifest.json').read_text())
    assert manifest['release_status']=='UNVALIDATED_PRIVATE_CANDIDATE'
    assert set(manifest['rows'])=={'customers','orders','order_items'}
    with pytest.raises(GenerationError,match='empty'):
        export(result,target)


def test_export_records_candidate_hash_bound_to_generation_run(tmp_path):
    result = generate(make_reference(20), backend='demo')
    target = tmp_path / 'candidate'
    export(result, target, provenance={
        'generation_run_id': 'generation-test-hash',
        'reference_parameters': {'customers': 20, 'seed': 2026},
        'reference_hash': 'a' * 64,
    })
    manifest = json.loads((target / 'generation_manifest.json').read_text())
    assert manifest['provenance']['candidate_content_hash'] == canonical_content_hash(
        result.tables, 'generation-test-hash'
    )


def test_sdv_adapter_injected_api_contract(monkeypatch):
    """SDV contract mock: requires PK-only inference and exactly two FK edges.

    This test does not replace the separate real-SDV integration test.
    """
    relationships = []
    inferred_modes = []

    class FakeMetadata:
        def __init__(self):
            self.schema = {
                name: {
                    'columns': {column: {'sdtype': 'id' if column.endswith('_id') else 'categorical'}
                                for column in table.columns},
                    'primary_key': None,
                }
                for name, table in make_reference(25).items()
            }

        @classmethod
        def detect_from_dataframes(cls, data, infer_keys='primary_and_foreign'):
            assert set(data) == {'customers', 'orders', 'order_items'}
            inferred_modes.append(infer_keys)
            return cls()

        def update_column(self, table_name, column_name, sdtype):
            self.schema[table_name]['columns'][column_name]['sdtype'] = sdtype

        def set_primary_key(self, table_name, column_name):
            self.schema[table_name]['primary_key'] = column_name

        def add_relationship(self, **relation):
            if relation in relationships:
                raise ValueError('Duplicate relationship')
            relationships.append(relation)

        def validate(self):
            pass

        def validate_data(self, data):
            pass

        def to_dict(self):
            return {'tables': self.schema, 'relationships': list(relationships)}

    class FakeHMA:
        def __init__(self, meta):
            pass

        def set_random_state(self, seed):
            pass

        def fit(self, tables):
            self.tables = tables

        def sample_scale(self, scale):
            return generate(self.tables, backend='demo', scale=scale).tables

    root = types.ModuleType('sdv')
    root.__path__ = []
    root.__version__ = 'fake-for-contract'
    meta = types.ModuleType('sdv.metadata')
    meta.Metadata = FakeMetadata
    multi = types.ModuleType('sdv.multi_table')
    multi.HMASynthesizer = FakeHMA
    monkeypatch.setitem(sys.modules, 'sdv', root)
    monkeypatch.setitem(sys.modules, 'sdv.metadata', meta)
    monkeypatch.setitem(sys.modules, 'sdv.multi_table', multi)

    result = generate(make_reference(25), backend='sdv')
    assert result.backend == 'sdv-hma'
    assert inferred_modes == ['primary_only']
    assert len(relationships) == 2
    assert {(x['parent_table_name'], x['child_table_name']) for x in relationships} == {
        ('customers', 'orders'), ('orders', 'order_items')
    }


def test_sdv_metadata_fails_closed_if_sdv_auto_detects_foreign_keys(monkeypatch):
    """Unexpected edges must not be accepted as valid metadata."""
    import gate.generate as module
    root = types.ModuleType('sdv')
    root.__path__ = []
    meta = types.ModuleType('sdv.metadata')

    class MetadataThatIgnoresInferenceMode:
        @classmethod
        def detect_from_dataframes(cls, data, infer_keys='primary_and_foreign'):
            assert infer_keys == 'primary_only'
            return cls()

        def to_dict(self):
            return {'tables': {}, 'relationships': [{'parent_table_name': 'customers'}]}

    meta.Metadata = MetadataThatIgnoresInferenceMode
    monkeypatch.setitem(sys.modules, 'sdv', root)
    monkeypatch.setitem(sys.modules, 'sdv.metadata', meta)
    with pytest.raises(GenerationError, match='unexpected relationships'):
        module._create_metadata(make_reference(15))


def test_broken_sdv_does_not_switch_to_demo(monkeypatch):
    import gate.generate as module
    def fail(*args,**kwargs): raise GenerationError('offline')
    monkeypatch.setattr(module,'_sdv_generate',fail)
    with pytest.raises(GenerationError,match='offline'):
        generate(make_reference(15),backend='sdv')
