"""Canonical, source-independent candidate snapshots and content hashes."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
from typing import Any, Mapping

import numpy as np
import pandas as pd


TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "customers": ("customer_id", "region", "age_group"),
    "orders": ("order_id", "customer_id", "order_date", "status"),
    "order_items": ("item_id", "order_id", "category", "quantity", "price"),
}
TABLE_ORDER = tuple(TABLE_COLUMNS)
NUMERIC_PRECISION = 38
NUMERIC_SCALE = 9
NUMERIC_QUANTUM = Decimal("1e-9")
NUMERIC_MAX_INTEGER = Decimal("1e29")


class CanonicalizationError(ValueError):
    """Raised when a candidate cannot be represented unambiguously."""


def validate_candidate_schema(tables: Mapping[str, pd.DataFrame]) -> None:
    if set(tables) != set(TABLE_COLUMNS):
        raise CanonicalizationError(
            f"Expected tables {sorted(TABLE_COLUMNS)}, got {sorted(tables)}"
        )
    for name in TABLE_ORDER:
        frame = tables[name]
        if not isinstance(frame, pd.DataFrame):
            raise CanonicalizationError(f"{name} is not a DataFrame")
        if not frame.columns.is_unique:
            raise CanonicalizationError(f"{name} has duplicate column names")
        expected = TABLE_COLUMNS[name]
        if tuple(frame.columns) != expected:
            raise CanonicalizationError(
                f"{name} schema mismatch: expected {expected}, got {tuple(frame.columns)}"
            )


def snapshot_tables(tables: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Validate and deep-copy all tables once for one publication attempt."""

    validate_candidate_schema(tables)
    return {name: tables[name].copy(deep=True).reset_index(drop=True) for name in TABLE_ORDER}


def _value_type(value: Any) -> str:
    if isinstance(value, (bool, np.bool_)):
        return "bool"
    if isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_)):
        return "int"
    if isinstance(value, (float, np.floating)):
        return "float"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return "datetime"
    if isinstance(value, str):
        return "string"
    raise CanonicalizationError(f"Unsupported or ambiguous value type: {type(value).__name__}")


def _canonical_value(value: Any) -> dict[str, str]:
    if value is None or value is pd.NA:
        raise CanonicalizationError("Missing values are not allowed in a publication snapshot")
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        missing = False
    if isinstance(missing, (bool, np.bool_)) and bool(missing):
        raise CanonicalizationError("NaN, infinity, and missing values are not allowed")

    kind = _value_type(value)
    if kind == "bool":
        return {"type": kind, "value": "1" if bool(value) else "0"}
    if kind == "int":
        return {"type": kind, "value": str(int(value))}
    if kind == "float":
        number = float(value)
        if not np.isfinite(number):
            raise CanonicalizationError("NaN, infinity, and missing values are not allowed")
        normalized = 0.0 if number == 0 else number
        return {"type": kind, "value": str(normalized)}
    if kind == "decimal":
        return {"type": kind, "value": format(value, "f")}
    if kind == "datetime":
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp):
            raise CanonicalizationError("Invalid datetime value")
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert("UTC")
        return {"type": kind, "value": timestamp.isoformat()}
    return {"type": kind, "value": value}


def _normalized_column_value(column: str, value: Any) -> Any:
    # CSV round-trips represent the schema's date field as text. Normalize it
    # before hashing so the generation snapshot and evaluated CSV snapshot are
    # equivalent without weakening malformed-date rejection.
    if column == "order_date" and isinstance(value, str):
        try:
            return pd.Timestamp(value)
        except ValueError as exc:
            raise CanonicalizationError("Invalid order_date value") from exc
    if column == "price":
        return canonical_numeric_decimal(value)
    return value


def canonical_numeric_decimal(value: Any) -> Decimal:
    """Normalize the schema's NUMERIC(38,9) price without lossy rounding.

    Decimal and integer inputs must already be representable at scale 9.
    Float inputs are accepted only when their exact binary value is within two
    ulps of a scale-9 decimal; this bridges ordinary float arithmetic noise
    without collapsing distinct valid NUMERIC values.
    """
    try:
        if isinstance(value, (bool, np.bool_)):
            raise CanonicalizationError("Boolean is not a valid NUMERIC price")
        # BigQuery NUMERIC permits 38 total digits.  The process-wide Decimal
        # context defaults to 28, so use an explicit context large enough to
        # validate the complete schema boundary without rounding it first.
        with localcontext() as context:
            context.prec = NUMERIC_PRECISION + 12
            if isinstance(value, Decimal):
                decimal_value = value
            elif isinstance(value, (float, np.floating)):
                number = float(value)
                if not np.isfinite(number):
                    raise CanonicalizationError("NaN, infinity, and missing values are not allowed")
                exact = Decimal.from_float(number)
                candidate = exact.quantize(NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN)
                ulp = Decimal(str(np.spacing(abs(number) or 1.0)))
                if abs(exact - candidate) > 2 * ulp:
                    raise CanonicalizationError("Float price has unsupported precision for NUMERIC(38,9)")
                decimal_value = candidate
            elif isinstance(value, (int, np.integer)):
                decimal_value = Decimal(int(value))
            else:
                raise CanonicalizationError(f"Unsupported NUMERIC price type: {type(value).__name__}")
            if not decimal_value.is_finite():
                raise CanonicalizationError("NaN, infinity, and missing values are not allowed")
            if abs(decimal_value) >= NUMERIC_MAX_INTEGER:
                raise CanonicalizationError("NUMERIC price exceeds the 29-digit integer bound")
            quantized = decimal_value.quantize(NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN)
            if quantized != decimal_value:
                raise CanonicalizationError("NUMERIC price has more than 9 fractional digits")
            digits = len(quantized.as_tuple().digits)
            if digits > NUMERIC_PRECISION:
                raise CanonicalizationError("NUMERIC price exceeds precision 38")
            return quantized
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise CanonicalizationError("Invalid NUMERIC price") from exc


def _logical_column_type(series: pd.Series, column: str) -> str:
    values = [_canonical_value(_normalized_column_value(column, value))["type"]
              for value in series.tolist()]
    if not values:
        if pd.api.types.is_datetime64_any_dtype(series):
            return "datetime"
        if pd.api.types.is_bool_dtype(series):
            return "bool"
        if pd.api.types.is_integer_dtype(series):
            return "int"
        if pd.api.types.is_float_dtype(series):
            return "float"
        return "string"
    if len(set(values)) != 1:
        raise CanonicalizationError(f"Mixed value types in column {series.name!r}")
    return values[0]


def canonical_payload(tables: Mapping[str, pd.DataFrame], run_id: str) -> dict[str, Any]:
    if not isinstance(run_id, str) or not run_id.strip():
        raise CanonicalizationError("run_id must be a non-empty string")
    validate_candidate_schema(tables)
    payload: dict[str, Any] = {"format": "srg-candidate-v1", "run_id": run_id, "tables": []}
    for name in TABLE_ORDER:
        frame = tables[name]
        rows = [
            [_canonical_value(_normalized_column_value(column, value))
             for column, value in zip(frame.columns, row)]
            for row in frame.itertuples(index=False, name=None)
        ]
        row_tokens = [json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows]
        payload["tables"].append(
            {
                "name": name,
                "columns": [
                    {"name": column, "type": _logical_column_type(frame[column], column)}
                    for column in frame.columns
                ],
                "row_count": len(frame),
                "rows": [json.loads(token) for token in sorted(row_tokens)],
            }
        )
    return payload


def canonical_content_hash(tables: Mapping[str, pd.DataFrame], run_id: str) -> str:
    encoded = json.dumps(
        canonical_payload(tables, run_id), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_reference_hash(
    tables: Mapping[str, pd.DataFrame], *, customers: int, seed: int
) -> str:
    """Hash a reference snapshot together with the parameters that created it.

    Unlike a candidate hash, this is a source-lineage fingerprint rather than a
    publication-run binding.  The parameters prevent two different generated
    references with coincidentally equal rows from being treated as equivalent.
    """
    if type(customers) is not int or customers < 12:
        raise CanonicalizationError("reference customers must be an integer >= 12")
    if type(seed) is not int or seed < 0:
        raise CanonicalizationError("reference seed must be a nonnegative integer")
    payload = {
        "format": "srg-reference-v1",
        "parameters": {"customers": customers, "seed": seed},
        "dataset": canonical_payload(tables, "reference-dataset"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
