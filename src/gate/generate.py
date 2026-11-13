"""Multi-table synthetic generation adapter: explicit SDV or demo-only fallback.

No automatic fallback is permitted: SDV failure must fail closed and be observable.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import inspect
import json
import random
import numpy as np
import pandas as pd
from gate.checks.hard import run_hard_checks, TABLE_KEYS
from gate.models import Status
from gate.canonical import canonical_content_hash

EXPECTED = frozenset(TABLE_KEYS)
REQUIRED_SCHEMA = {
    'customers': frozenset({'customer_id', 'region', 'age_group'}),
    'orders': frozenset({'order_id', 'customer_id', 'order_date', 'status'}),
    'order_items': frozenset({'item_id', 'order_id', 'category', 'quantity', 'price'}),
}

class GenerationError(RuntimeError):
    pass

@dataclass
class GenerationResult:
    tables: dict[str, pd.DataFrame]
    backend: str
    metadata: dict


def _validate(tables: dict[str,pd.DataFrame], label: str) -> None:
    if not isinstance(tables,dict) or set(tables) != EXPECTED:
        raise GenerationError(f'{label}: expected exactly {sorted(EXPECTED)}')
    for name, df in tables.items():
        if not isinstance(df,pd.DataFrame) or df.empty:
            raise GenerationError(f'{label}.{name}: nonempty DataFrame required')
        missing = REQUIRED_SCHEMA[name] - set(df.columns)
        if missing:
            raise GenerationError(f'{label}.{name}: missing required columns {sorted(missing)}')
    bad = [c for c in run_hard_checks(tables) if c.status is not Status.PASS]
    if bad:
        raise GenerationError(f'{label}: failed structural validation: {bad}')


def _create_metadata(tables):
    """Build explicit SDV metadata without inferring FK relationships twice.

    SDV detects relationships by default based on column names. For this fixed
    schema, discover column types and possible PKs, but declare exactly the two
    supported FK edges ourselves. Never silently accept an unexpected edge.
    """
    from sdv.metadata import Metadata

    if not hasattr(Metadata, 'detect_from_dataframes'):
        raise GenerationError('Installed SDV Metadata API is unsupported')

    expected_edges = [
        ('customers', 'orders', 'customer_id'),
        ('orders', 'order_items', 'order_id'),
    ]
    expected = {
        (parent, TABLE_KEYS[parent], child, foreign)
        for parent, child, foreign in expected_edges
    }

    # SDV 1.17 has no infer_keys parameter and detects these valid edges;
    # newer SDV releases support primary_only and leave relationship creation
    # to the caller. Support both without ever adding an existing edge.
    detect_parameters = inspect.signature(Metadata.detect_from_dataframes).parameters
    if 'infer_keys' in detect_parameters:
        metadata = Metadata.detect_from_dataframes(data=tables, infer_keys='primary_only')
    else:
        metadata = Metadata.detect_from_dataframes(data=tables)

    def relationship_set(metadata_object):
        relationships = metadata_object.to_dict().get('relationships', [])
        required_keys = {
            'parent_table_name', 'parent_primary_key',
            'child_table_name', 'child_foreign_key',
        }
        if any(not required_keys.issubset(rel) for rel in relationships):
            raise GenerationError('SDV inferred unexpected relationships')
        observed = {(rel['parent_table_name'], rel['parent_primary_key'],
                     rel['child_table_name'], rel['child_foreign_key'])
                    for rel in relationships}
        return relationships, observed

    inferred_relationships, observed = relationship_set(metadata)
    if observed and observed != expected:
        raise GenerationError('SDV inferred unexpected relationships')

    id_fields = {
        'customers': ['customer_id'],
        'orders': ['order_id', 'customer_id'],
        'order_items': ['item_id', 'order_id'],
    }

    # Only update types or keys where necessary. Calling update_column on an
    # existing PK can unset it and emit avoidable warnings in SDV 2.0.
    for table_name, columns in id_fields.items():
        for column_name in columns:
            current = metadata.to_dict()['tables'][table_name]['columns'][column_name]
            if current['sdtype'] != 'id':
                metadata.update_column(
                    table_name=table_name, column_name=column_name, sdtype='id'
                )

    for table_name, primary_key in TABLE_KEYS.items():
        current_pk = metadata.to_dict()['tables'][table_name].get('primary_key')
        if current_pk != primary_key:
            metadata.set_primary_key(table_name=table_name, column_name=primary_key)

    if not inferred_relationships:
        for parent, child, foreign in expected_edges:
            metadata.add_relationship(
                parent_table_name=parent,
                child_table_name=child,
                parent_primary_key=TABLE_KEYS[parent],
                child_foreign_key=foreign,
            )

    # Check the resulting structure before training. A future change to SDV
    # must not silently introduce/miss a relationship.
    actual = metadata.to_dict()
    relationships = actual.get('relationships', [])
    observed = {
        (rel['parent_table_name'], rel['parent_primary_key'],
         rel['child_table_name'], rel['child_foreign_key'])
        for rel in relationships
    }
    if len(relationships) != len(expected) or observed != expected:
        raise GenerationError('SDV metadata relationships do not match the required schema')

    metadata.validate()
    # Both calls are supported by the SDV metadata API used by this adapter;
    # validation must complete before the synthesizer is constructed.
    metadata.validate_data(data=tables)
    return metadata


def _sdv_generate(tables:dict[str,pd.DataFrame], *, seed:int, scale:float) -> GenerationResult:
    try:
        import sdv
        from sdv.multi_table import HMASynthesizer
    except ImportError as exc:
        raise GenerationError('SDV is not installed: install with pip install -e ".[sdv]"') from exc
    try:
        metadata = _create_metadata(tables)
        synthesizer = HMASynthesizer(metadata)
        # SDV 1.x uses sample(scale=); SDV 2.x has sample_scale(scale=).
        if hasattr(synthesizer, 'set_random_state'):
            synthesizer.set_random_state(seed)
        else:
            np.random.seed(seed)
            random.seed(seed)
        synthesizer.fit(tables)
        if hasattr(synthesizer,'sample_scale'):
            output = synthesizer.sample_scale(scale=scale)
        elif 'scale' in inspect.signature(synthesizer.sample).parameters:
            output = synthesizer.sample(scale=scale)
        else:
            # Explicit compatibility failure, not silent demo substitution.
            raise GenerationError('SDV version has no supported multi-table scale sampler')
        meta_dict = metadata.to_dict()
        return GenerationResult(output,'sdv-hma',{'sdv_version':getattr(sdv,'__version__','unknown'),
                                                'metadata':meta_dict,'seed':seed,'scale':scale})
    except GenerationError:
        raise
    except Exception as exc:
        raise GenerationError(f'SDV generation failed: {type(exc).__name__}: {exc}') from exc


def _demo_generate(tables:dict[str,pd.DataFrame], *,seed:int,scale:float) -> GenerationResult:
    """Lightweight relational resampler for CI/demo; NOT an SDV equivalent."""
    rng=np.random.default_rng(seed)
    parent=tables['customers']; orders=tables['orders']; items=tables['order_items']
    selected=rng.choice(parent['customer_id'].to_numpy(),size=max(1,int(round(len(parent)*scale))),replace=True)
    c_rows=[]; o_rows=[]; i_rows=[]
    for j,old_cid in enumerate(selected,1):
        c=parent.loc[parent.customer_id==old_cid].iloc[0].to_dict(); c['customer_id']=f'SC{j:08d}'
        c_rows.append(c)
        old_orders=orders.loc[orders.customer_id==old_cid]
        if old_orders.empty: continue
        for _,old_o in old_orders.iterrows():
            oid=f'SO{len(o_rows)+1:09d}'; row=old_o.to_dict()
            row.update(order_id=oid,customer_id=c['customer_id']); o_rows.append(row)
            for _,old_item in items.loc[items.order_id==old_o['order_id']].iterrows():
                item=old_item.to_dict(); item.update(item_id=f'SI{len(i_rows)+1:010d}',order_id=oid)
                i_rows.append(item)
    result={'customers':pd.DataFrame(c_rows),'orders':pd.DataFrame(o_rows),'order_items':pd.DataFrame(i_rows)}
    return GenerationResult(result,'demo-relational-bootstrap',{'seed':seed,'scale':scale,'warning':'NOT SDV; resamples source patterns and is NOT privacy safe'})


def generate(tables:dict[str,pd.DataFrame], *,backend:str='sdv',seed:int=2026,scale:float=1.0) -> GenerationResult:
    if type(seed) is not int or seed < 0 or type(scale) not in (float,int) or not np.isfinite(scale) or scale <= 0:
        raise GenerationError('Invalid seed or scale')
    _validate(tables,'source')
    if backend == 'sdv':
        result=_sdv_generate(tables,seed=seed,scale=float(scale))
    elif backend == 'demo':
        result=_demo_generate(tables,seed=seed,scale=float(scale))
    else:
        raise GenerationError('backend must explicitly be sdv or demo')
    for name in EXPECTED:
        if set(result.tables[name].columns) != set(tables[name].columns):
            raise GenerationError(f'generated.{name}: schema mismatch against source')
    _validate(result.tables,'generated')
    return result


def export(result:GenerationResult, output_dir:str|Path, *, provenance: dict | None = None) -> Path:
    """Write artifact and manifest; never mark it approved or publish it."""
    path=Path(output_dir)
    if path.exists() and any(path.iterdir()):
        raise GenerationError('Output directory must be empty (no accidental overwrite)')
    path.mkdir(parents=True,exist_ok=True)
    for name in sorted(EXPECTED):
        result.tables[name].to_csv(path/f'{name}.csv',index=False)
    manifest={'backend':result.backend,'rows':{k:len(v) for k,v in result.tables.items()},
              'generation_metadata':result.metadata,'release_status':'UNVALIDATED_PRIVATE_CANDIDATE'}
    if provenance is not None:
        provenance = dict(provenance)
        generation_run_id = provenance.get('generation_run_id')
        if isinstance(generation_run_id, str) and generation_run_id.strip():
            provenance['candidate_content_hash'] = canonical_content_hash(
                result.tables, generation_run_id
            )
        manifest['provenance'] = provenance
    (path/'generation_manifest.json').write_text(json.dumps(manifest,indent=2,default=str),encoding='utf-8')
    return path
