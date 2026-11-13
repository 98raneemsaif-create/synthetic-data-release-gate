import pytest
from gate.reference import make_reference
from gate.generate import generate,GenerationError


def test_sdv_output_missing_column_is_rejected(monkeypatch):
    import gate.generate as module
    def malformed(tables,*,seed,scale):
        out=module._demo_generate(tables,seed=seed,scale=scale)
        out.tables['customers'].drop(columns=['region'],inplace=True)
        return out
    monkeypatch.setattr(module,'_sdv_generate',malformed)
    with pytest.raises(GenerationError,match='schema mismatch'):
        generate(make_reference(20),backend='sdv')
