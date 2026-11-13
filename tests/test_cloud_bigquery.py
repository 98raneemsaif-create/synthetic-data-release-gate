from types import SimpleNamespace

import pytest

from gate.cloud_bigquery import (
    BigQueryAdapterError,
    BigQueryPublicationAdapter,
    EXPECTED_FIELDS,
)


class MissingTable(Exception):
    code = 404


class ReadOnlyFakeClient:
    def get_table(self, _reference):
        raise MissingTable()


def test_target_inspection_is_read_only_and_reports_all_five_missing_tables():
    result = BigQueryPublicationAdapter(ReadOnlyFakeClient()).inspect_target_tables()
    assert set(result) == set(EXPECTED_FIELDS)
    assert all(not item["exists"] for item in result.values())


def test_existing_schema_collision_fails_closed():
    class WrongClient:
        def get_table(self, _reference):
            return SimpleNamespace(schema=[SimpleNamespace(name="wrong", field_type="STRING")])

    with pytest.raises(BigQueryAdapterError, match="schema collision"):
        BigQueryPublicationAdapter(WrongClient()).inspect_target_tables()


def test_struct_parameter_is_built_with_explicit_fields():
    adapter = BigQueryPublicationAdapter(ReadOnlyFakeClient())
    parameter = adapter._struct_array(
        "customers", (("customer_id", "STRING"), ("region", "STRING")),
        [{"customer_id": "c1", "region": "R"}],
    )
    representation = parameter.to_api_repr()
    assert representation["name"] == "customers"
    assert representation["parameterType"]["type"] == "ARRAY"
    assert representation["parameterType"]["arrayType"]["type"] == "STRUCT"
    assert len(representation["parameterValue"]["arrayValues"]) == 1
