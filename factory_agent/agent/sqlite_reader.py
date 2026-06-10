from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from app_common.schemas import ColumnSchema, DatabaseSchemaSnapshot, ForeignKeySchema, TableSchema

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("SQLite identifier must be a non-empty string")
    if not _IDENTIFIER_RE.match(name):
        # SQLite supports quoted identifiers. Escaping double quotes is enough here.
        return '"' + name.replace('"', '""') + '"'
    return '"' + name + '"'


def create_sqlite_snapshot(source_path: str | Path) -> Path:
    """
    Create a consistent SQLite snapshot using SQLite backup API.
    This avoids long reads against the live factory DB.
    """
    source = Path(source_path)
    fd, tmp_name = tempfile.mkstemp(prefix="factory_snapshot_", suffix=".db")
    os.close(fd)
    tmp = Path(tmp_name)

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return tmp


@contextmanager
def readable_db(sqlite_path: str | Path, use_snapshot: bool) -> Iterator[Path]:
    snapshot: Path | None = None
    if use_snapshot:
        snapshot = create_sqlite_snapshot(sqlite_path)
        try:
            yield snapshot
        finally:
            gc.collect()
            for _ in range(10):
                try:
                    snapshot.unlink(missing_ok=True)
                    break
                except PermissionError:
                    time.sleep(0.2)
            if snapshot.exists():
                print(f"Warning: could not delete temporary SQLite snapshot: {snapshot}")
    else:
        yield Path(sqlite_path)


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_tables(db_path: str | Path, include: list[str], exclude: list[str]) -> list[str]:
    conn = connect_readonly(db_path)
    try:
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        tables = [str(row["name"]) for row in rows]
    finally:
        conn.close()

    def is_included(table: str) -> bool:
        return "*" in include or table in include

    return [t for t in tables if is_included(t) and t not in exclude]


def inspect_table_schema(conn: sqlite3.Connection, table_name: str) -> TableSchema:
    q_table = quote_identifier(table_name)
    col_rows = conn.execute(f"PRAGMA table_info({q_table})").fetchall()
    columns = [
        ColumnSchema(
            cid=int(row["cid"]),
            name=str(row["name"]),
            type=row["type"],
            notnull=bool(row["notnull"]),
            default_value=row["dflt_value"],
            primary_key_position=int(row["pk"]),
        )
        for row in col_rows
    ]
    fk_rows = conn.execute(f"PRAGMA foreign_key_list({q_table})").fetchall()
    foreign_keys = [
        ForeignKeySchema(
            id=int(row["id"]),
            seq=int(row["seq"]),
            table=str(row["table"]),
            from_column=str(row["from"]),
            to_column=row["to"],
            on_update=row["on_update"],
            on_delete=row["on_delete"],
            match=row["match"],
        )
        for row in fk_rows
    ]
    primary_key_columns = [c.name for c in sorted(columns, key=lambda c: c.primary_key_position) if c.primary_key_position]
    fingerprint_payload = {
        "table_name": table_name,
        "columns": [c.model_dump(mode="json") for c in columns],
        "foreign_keys": [fk.model_dump(mode="json") for fk in foreign_keys],
        "primary_key_columns": primary_key_columns,
    }
    schema_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return TableSchema(
        table_name=table_name,
        columns=columns,
        foreign_keys=foreign_keys,
        primary_key_columns=primary_key_columns,
        schema_fingerprint=schema_fingerprint,
    )


def inspect_database_schema(db_path: str | Path, include: list[str], exclude: list[str]) -> DatabaseSchemaSnapshot:
    conn = connect_readonly(db_path)
    try:
        table_names = list_tables(db_path, include, exclude)
        tables = {name: inspect_table_schema(conn, name) for name in table_names}
    finally:
        conn.close()

    db_payload = {
        "tables": {name: table.schema_fingerprint for name, table in sorted(tables.items())},
    }
    database_fingerprint = hashlib.sha256(
        json.dumps(db_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return DatabaseSchemaSnapshot(
        database_fingerprint=database_fingerprint,
        scanned_at=datetime.now(timezone.utc),
        tables=tables,
    )


def column_names(schema: TableSchema) -> list[str]:
    return [c.name for c in schema.columns]


def choose_sync_strategy(
    schema: TableSchema,
    *,
    id_candidates: list[str],
    updated_at_candidates: list[str],
    timestamp_candidates: list[str],
) -> tuple[str, str | None]:
    cols = column_names(schema)
    lower_to_real = {c.lower(): c for c in cols}

    for candidate in id_candidates:
        real = lower_to_real.get(candidate.lower())
        if real:
            return "id_incremental", real

    for candidate in updated_at_candidates:
        real = lower_to_real.get(candidate.lower())
        if real:
            return "updated_at_incremental", real

    for candidate in timestamp_candidates:
        real = lower_to_real.get(candidate.lower())
        if real:
            return "timestamp_incremental", real

    if len(schema.primary_key_columns) == 1:
        return "id_incremental", schema.primary_key_columns[0]

    return "full_snapshot", None


def get_table_row_count(db_path: str | Path, table: str) -> int:
    conn = connect_readonly(db_path)
    try:
        q_table = quote_identifier(table)
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {q_table}").fetchone()
        return int(row["c"])
    finally:
        conn.close()


def get_max_value(db_path: str | Path, table: str, column: str) -> Any:
    conn = connect_readonly(db_path)
    try:
        q_table = quote_identifier(table)
        q_col = quote_identifier(column)
        row = conn.execute(f"SELECT MAX({q_col}) AS max_value FROM {q_table}").fetchone()
        return row["max_value"] if row else None
    finally:
        conn.close()


def read_full_table(db_path: str | Path, table: str, limit: int | None = None) -> pd.DataFrame:
    q_table = quote_identifier(table)
    sql = f"SELECT * FROM {q_table}"
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    conn = connect_readonly(db_path)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def read_incremental_rows(
    db_path: str | Path,
    table: str,
    sync_key: str,
    strategy: str,
    last_value: Any,
    limit: int,
    *,
    timestamp_overlap_seconds: int = 0,
) -> pd.DataFrame:
    q_table = quote_identifier(table)
    q_key = quote_identifier(sync_key)
    params: list[Any] = []

    if last_value is None:
        where_sql = "1=1"
    elif strategy in {"updated_at_incremental", "timestamp_incremental"}:
        value = str(last_value)
        if timestamp_overlap_seconds:
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                value = (dt - timedelta(seconds=timestamp_overlap_seconds)).isoformat()
            except ValueError:
                pass
        where_sql = f"{q_key} >= ?"
        params.append(value)
    else:
        where_sql = f"{q_key} > ?"
        params.append(last_value)

    sql = f"SELECT * FROM {q_table} WHERE {where_sql} ORDER BY {q_key} ASC LIMIT ?"
    params.append(int(limit))
    conn = connect_readonly(db_path)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def get_table_content_fingerprint(
    db_path: str | Path,
    table: str,
    schema: TableSchema,
    *,
    limit: int | None = None,
) -> str:
    """
    Create a stable content fingerprint for a table snapshot.

    This is intentionally used only for explicit full-snapshot requests, where
    reading the table is already expected. It helps avoid storing duplicate full
    snapshots when the user repeats the same full database request.
    """
    q_table = quote_identifier(table)
    column_list = [c.name for c in schema.columns]
    q_columns = ", ".join(quote_identifier(c) for c in column_list) if column_list else "*"

    order_columns = schema.primary_key_columns or []
    if order_columns:
        order_sql = ", ".join(quote_identifier(c) for c in order_columns)
    else:
        order_sql = "rowid"

    sql = f"SELECT {q_columns} FROM {q_table} ORDER BY {order_sql}"
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    digest = hashlib.sha256()
    digest.update(schema.schema_fingerprint.encode("utf-8"))

    conn = connect_readonly(db_path)
    try:
        try:
            cursor = conn.execute(sql, params)
        except sqlite3.OperationalError:
            # Some SQLite tables may not expose rowid. Fall back to an unordered
            # scan. This is less ideal, but still gives a practical duplicate
            # guard for normal local testing and many factory databases.
            sql = f"SELECT {q_columns} FROM {q_table}"
            if limit is not None:
                sql += " LIMIT ?"
            cursor = conn.execute(sql, params)

        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                payload = {key: row[key] for key in row.keys()}
                digest.update(json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
                digest.update(b"\n")
    finally:
        conn.close()

    return digest.hexdigest()


def validate_table_and_columns(schema: TableSchema, columns: list[str] | None) -> list[str]:
    available = {c.name for c in schema.columns}
    if not columns:
        return [c.name for c in schema.columns]
    invalid = [c for c in columns if c not in available]
    if invalid:
        raise ValueError(f"Unknown column(s) for table {schema.table_name}: {', '.join(invalid)}")
    return columns


def column_type(schema: TableSchema, column_name: str) -> str:
    for column in schema.columns:
        if column.name == column_name:
            return (column.type or "").upper()
    return ""


def normalize_filter_value_for_column(schema: TableSchema, column_name: str, value: Any) -> Any:
    """
    Convert numeric filter values when the target SQLite column is numeric.

    SQLite is dynamically typed, but converting obvious numeric inputs prevents
    cases where a text value leads to unexpected comparison behavior.
    """
    if value is None:
        return value
    text = str(value).strip()
    declared_type = column_type(schema, column_name)
    if any(token in declared_type for token in ["INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"]):
        if text == "":
            return value
        try:
            if any(ch in text for ch in [".", "e", "E"]):
                return float(text)
            return int(text)
        except ValueError:
            return value
    return value


def sqlite_datetime_expression(quoted_column: str) -> str:
    """
    Normalize common SQLite text timestamp formats for comparison.

    Supports values like:
    - 2026-06-07 12:30:00
    - 2026-06-07T12:30:00
    - 2026-06-07T12:30:00Z
    """
    return f"datetime(replace(replace(CAST({quoted_column} AS TEXT), 'T', ' '), 'Z', ''))"



def normalize_sql_operator(operator: str | None) -> str:
    op = (operator or "eq").lower().strip()
    aliases = {
        "equals": "eq",
        "not_equals": "ne",
        "not equals": "ne",
        "greater_than": "gt",
        "greater than": "gt",
        "greater_than_or_equal": "gte",
        "greater than or equal": "gte",
        "less_than": "lt",
        "less than": "lt",
        "less_than_or_equal": "lte",
        "less than or equal": "lte",
        "contains text": "contains",
    }
    return aliases.get(op, op)


def build_filter_condition(
    *,
    schema: TableSchema,
    column: str,
    operator: str | None,
    value: Any,
) -> tuple[str, list[Any]]:
    available = {c.name for c in schema.columns}
    if column not in available:
        raise ValueError(f"Unknown filter column for table {schema.table_name}: {column}")
    if value in (None, ""):
        raise ValueError(f"Filter value for column {column} is empty.")

    op = normalize_sql_operator(operator)
    q_col = quote_identifier(column)
    normalized_value = normalize_filter_value_for_column(schema, column, value)

    if op == "eq":
        return f"{q_col} = ?", [normalized_value]
    if op == "ne":
        return f"{q_col} <> ?", [normalized_value]
    if op == "gt":
        return f"{q_col} > ?", [normalized_value]
    if op == "gte":
        return f"{q_col} >= ?", [normalized_value]
    if op == "lt":
        return f"{q_col} < ?", [normalized_value]
    if op == "lte":
        return f"{q_col} <= ?", [normalized_value]
    if op == "contains":
        return f"CAST({q_col} AS TEXT) LIKE ?", [f"%{value}%"]
    raise ValueError("Unsupported operator. Use one of: eq, ne, gt, gte, lt, lte, contains")


def sqlite_datetime_expression(quoted_column: str) -> str:
    """
    Normalize common SQLite timestamp formats for comparison.

    This expression supports common text timestamps. Numeric unix timestamps are
    also handled by trying SQLite's unixepoch modifier when the value looks
    numeric. The comparison still stays fully parameterized.
    """
    return (
        "CASE "
        f"WHEN typeof({quoted_column}) IN ('integer','real') THEN datetime({quoted_column}, 'unixepoch') "
        f"WHEN CAST({quoted_column} AS TEXT) GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*' "
        f"THEN datetime(CAST({quoted_column} AS INTEGER), 'unixepoch') "
        f"ELSE datetime(replace(replace(CAST({quoted_column} AS TEXT), 'T', ' '), 'Z', '')) "
        "END"
    )


def read_limited_query(
    db_path: str | Path,
    *,
    schema: TableSchema,
    columns: list[str] | None = None,
    where_column: str | None = None,
    operator: str | None = None,
    value: Any = None,
    filters: list[dict[str, Any]] | None = None,
    time_column: str | None = None,
    start_time: Any = None,
    end_time: Any = None,
    limit: int = 1000,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Execute a safe custom single-table query.

    The UI may send either one legacy value filter or a list of filters. Both
    paths are validated against the discovered table schema. Free-form SQL is
    never accepted.
    """
    selected_columns = validate_table_and_columns(schema, columns)
    if not selected_columns:
        raise ValueError("Select at least one column for the custom query.")

    available = {c.name for c in schema.columns}
    where_parts: list[str] = []
    params: list[Any] = []
    applied_filters: list[dict[str, Any]] = []

    filter_items = filters or []
    if not filter_items and where_column:
        filter_items = [{"column": where_column, "operator": operator or "eq", "value": value}]
    elif value not in (None, "") and not where_column and not filter_items:
        raise ValueError("A value filter was provided, but no value filter column was selected.")

    first_time_column_for_order: str | None = None

    for item in filter_items:
        column = item.get("column")
        if not column:
            if not any(item.get(k) not in (None, "") for k in ("value", "start_time", "end_time")):
                continue
            raise ValueError("A filter value was provided, but no filter column was selected.")

        filter_type = item.get("filter_type") or item.get("type") or "value"
        has_item_time_range = item.get("start_time") not in (None, "") or item.get("end_time") not in (None, "")

        if filter_type == "time" or has_item_time_range:
            if str(column) not in available:
                raise ValueError(f"Unknown time column for table {schema.table_name}: {column}")
            start_value = item.get("start_time")
            end_value = item.get("end_time")
            if start_value in (None, "") and end_value in (None, ""):
                raise ValueError(f"Time filter for column {column} needs a start or end time.")
            q_time = quote_identifier(str(column))
            time_expr = sqlite_datetime_expression(q_time)
            if start_value not in (None, ""):
                where_parts.append(f"{time_expr} >= datetime(?)")
                params.append(str(start_value))
            if end_value not in (None, ""):
                where_parts.append(f"{time_expr} <= datetime(?)")
                params.append(str(end_value))
            applied_filters.append({
                "column": column,
                "filter_type": "time",
                "start_time": start_value,
                "end_time": end_value,
            })
            if first_time_column_for_order is None:
                first_time_column_for_order = str(column)
            continue

        op = item.get("operator") or "eq"
        val = item.get("value")
        condition, condition_params = build_filter_condition(
            schema=schema,
            column=str(column),
            operator=str(op),
            value=val,
        )
        where_parts.append(condition)
        params.extend(condition_params)
        applied_filters.append({"column": column, "operator": normalize_sql_operator(str(op)), "value": val})

    # Backward compatibility for the old single time filter fields.
    has_time_filter = start_time not in (None, "") or end_time not in (None, "")
    if has_time_filter and not time_column:
        raise ValueError("A time range was provided, but no time column was selected.")
    if time_column and not has_time_filter:
        raise ValueError("A time column was selected, but no start or end time was provided.")

    if time_column:
        if time_column not in available:
            raise ValueError(f"Unknown time column for table {schema.table_name}: {time_column}")
        q_time = quote_identifier(time_column)
        time_expr = sqlite_datetime_expression(q_time)
        if start_time not in (None, ""):
            where_parts.append(f"{time_expr} >= datetime(?)")
            params.append(str(start_time))
        if end_time not in (None, ""):
            where_parts.append(f"{time_expr} <= datetime(?)")
            params.append(str(end_time))
        applied_filters.append({
            "column": time_column,
            "filter_type": "time",
            "start_time": start_time,
            "end_time": end_time,
        })
        if first_time_column_for_order is None:
            first_time_column_for_order = str(time_column)

    q_table = quote_identifier(schema.table_name)
    q_columns = ", ".join(quote_identifier(c) for c in selected_columns)
    sql = f"SELECT {q_columns} FROM {q_table}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)

    if schema.primary_key_columns:
        sql += " ORDER BY " + ", ".join(quote_identifier(c) for c in schema.primary_key_columns)
    elif first_time_column_for_order:
        sql += " ORDER BY " + quote_identifier(first_time_column_for_order)

    safe_limit = max(1, min(int(limit or 1000), 1_000_000))
    sql += " LIMIT ?"
    params.append(safe_limit)

    query_info = {
        "table": schema.table_name,
        "columns": selected_columns,
        "filters": applied_filters,
        "limit": safe_limit,
        "sql_template": sql,
        "parameters": params[:-1],
    }

    conn = connect_readonly(db_path)
    try:
        df = pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()
    return df, query_info
