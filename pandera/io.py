"""Module for reading and writing schema objects."""

import warnings
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Dict, Optional, Union

import pandas as pd

import pandera.errors

from . import dtypes
from .checks import Check
from .engines import pandas_engine
from .schema_components import Column
from .schema_statistics import get_dataframe_schema_statistics
from .schemas import DataFrameSchema

try:
    import black
    import yaml
    from frictionless import Schema as FrictionlessSchema
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "IO and formatting requires 'pyyaml', 'black' and 'frictionless'"
        "to be installed.\n"
        "You can install pandera together with the IO dependencies with:\n"
        "pip install pandera[io]\n"
    ) from exc


DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _get_qualified_name(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _serialize_check_stats(check_stats, dtype=None):
    """Serialize check statistics into json/yaml-compatible format."""

    def handle_stat_dtype(stat):
        if pandas_engine.Engine.dtype(dtypes.DateTime).check(
            dtype
        ) and hasattr(stat, "strftime"):
            # try serializing stat as a string if it's datetime-like,
            # otherwise return original value
            return stat.strftime(DATETIME_FORMAT)
        elif pandas_engine.Engine.dtype(dtypes.Timedelta).check(
            dtype
        ) and hasattr(stat, "delta"):
            # try serializing stat into an int in nanoseconds if it's
            # timedelta-like, otherwise return original value
            return stat.delta

        return stat

    # for unary checks, return a single value instead of a dictionary
    if len(check_stats) == 1:
        return handle_stat_dtype(list(check_stats.values())[0])

    # otherwise return a dictionary of keyword args needed to create the Check
    serialized_check_stats = {}
    for arg, stat in check_stats.items():
        serialized_check_stats[arg] = handle_stat_dtype(stat)
    return serialized_check_stats


def _serialize_dataframe_stats(dataframe_checks):
    """
    Serialize global dataframe check statistics into json/yaml-compatible
    format.
    """
    serialized_checks = {}

    for check_name, check_stats in dataframe_checks.items():
        # The case that `check_name` is not registered is handled in
        # `parse_checks` so we know that `check_name` exists.

        # infer dtype of statistics and serialize them
        serialized_checks[check_name] = _serialize_check_stats(check_stats)

    return serialized_checks


def _serialize_component_stats(component_stats):
    """
    Serialize column or index statistics into json/yaml-compatible format.
    """
    serialized_checks = None
    if component_stats["checks"] is not None:
        serialized_checks = {}
        for check_name, check_stats in component_stats["checks"].items():
            serialized_checks[check_name] = _serialize_check_stats(
                check_stats, component_stats["dtype"]
            )

    dtype = component_stats.get("dtype")
    if dtype:
        dtype = str(dtype)

    return {
        "dtype": dtype,
        "nullable": component_stats["nullable"],
        "checks": serialized_checks,
        **{
            key: component_stats.get(key)
            for key in [
                "name",
                "unique",
                "coerce",
                "required",
                "regex",
            ]
            if key in component_stats
        },
    }


def _serialize_schema(dataframe_schema):
    """Serialize dataframe schema into into json/yaml-compatible format."""
    from pandera import __version__  # pylint: disable=import-outside-toplevel

    statistics = get_dataframe_schema_statistics(dataframe_schema)

    columns, index, checks = None, None, None
    if statistics["columns"] is not None:
        columns = {
            col_name: _serialize_component_stats(column_stats)
            for col_name, column_stats in statistics["columns"].items()
        }

    if statistics["index"] is not None:
        index = [
            _serialize_component_stats(index_stats)
            for index_stats in statistics["index"]
        ]

    if statistics["checks"] is not None:
        checks = _serialize_dataframe_stats(statistics["checks"])

    return {
        "schema_type": "dataframe",
        "version": __version__,
        "columns": columns,
        "checks": checks,
        "index": index,
        "coerce": dataframe_schema.coerce,
        "strict": dataframe_schema.strict,
        "unique": dataframe_schema.unique,
    }


def _deserialize_check_stats(check, serialized_check_stats, dtype=None):
    def handle_stat_dtype(stat):
        try:
            if pandas_engine.Engine.dtype(dtypes.DateTime).check(dtype):
                return pd.to_datetime(stat, format=DATETIME_FORMAT)
            elif pandas_engine.Engine.dtype(dtypes.Timedelta).check(dtype):
                # serialize to int in nanoseconds
                return pd.to_timedelta(stat, unit="ns")
        except (TypeError, ValueError):
            return stat
        return stat

    if isinstance(serialized_check_stats, dict):
        # handle case where serialized check stats are in the form of a
        # dictionary mapping Check arg names to values.
        check_stats = {}
        for arg, stat in serialized_check_stats.items():
            check_stats[arg] = handle_stat_dtype(stat)
        return check(**check_stats)
    # otherwise assume unary check function signature
    return check(handle_stat_dtype(serialized_check_stats))


def _deserialize_component_stats(serialized_component_stats):
    dtype = serialized_component_stats.get("dtype")
    if dtype:
        dtype = pandas_engine.Engine.dtype(dtype)

    checks = serialized_component_stats.get("checks")
    if checks is not None:
        checks = [
            _deserialize_check_stats(
                getattr(Check, check_name), check_stats, dtype
            )
            for check_name, check_stats in checks.items()
        ]
    return {
        "dtype": dtype,
        "checks": checks,
        **{
            key: serialized_component_stats.get(key)
            for key in [
                "name",
                "nullable",
                "unique",
                # deserialize allow_duplicates property for backwards
                # compatibility. Remove this for 0.8.0 release
                "allow_duplicates",
                "coerce",
                "required",
                "regex",
            ]
            if key in serialized_component_stats
        },
    }


def _deserialize_schema(serialized_schema):
    # pylint: disable=import-outside-toplevel
    from pandera import Index, MultiIndex

    # GH#475
    serialized_schema = serialized_schema if serialized_schema else {}

    if not isinstance(serialized_schema, Mapping):
        raise pandera.errors.SchemaDefinitionError(
            "Schema representation must be a mapping."
        )

    columns = serialized_schema.get("columns")
    index = serialized_schema.get("index")
    checks = serialized_schema.get("checks")

    if columns is not None:
        columns = {
            col_name: Column(**_deserialize_component_stats(column_stats))
            for col_name, column_stats in columns.items()
        }

    if index is not None:
        index = [
            _deserialize_component_stats(index_component)
            for index_component in index
        ]

    if checks is not None:
        # handles unregistered checks by raising AttributeErrors from getattr
        checks = [
            _deserialize_check_stats(getattr(Check, check_name), check_stats)
            for check_name, check_stats in checks.items()
        ]

    if index is None:
        pass
    elif len(index) == 1:
        index = Index(**index[0])
    else:
        index = MultiIndex(
            indexes=[Index(**index_properties) for index_properties in index]
        )

    return DataFrameSchema(
        columns=columns,
        checks=checks,
        index=index,
        coerce=serialized_schema.get("coerce", False),
        strict=serialized_schema.get("strict", False),
        unique=serialized_schema.get("unique", None),
    )


def from_yaml(yaml_schema):
    """Create :class:`~pandera.schemas.DataFrameSchema` from yaml file.

    :param yaml_schema: str or Path to yaml schema, or serialized yaml string.
    :returns: dataframe schema.
    """
    try:
        with Path(yaml_schema).open("r", encoding="utf-8") as f:
            serialized_schema = yaml.safe_load(f)
    except (TypeError, OSError):
        serialized_schema = yaml.safe_load(yaml_schema)
    return _deserialize_schema(serialized_schema)


def to_yaml(dataframe_schema, stream=None):
    """Write :class:`~pandera.schemas.DataFrameSchema` to yaml file.

    :param dataframe_schema: schema to write to file or dump to string.
    :param stream: file stream to write to. If None, dumps to string.
    :returns: yaml string if stream is None, otherwise returns None.
    """
    statistics = _serialize_schema(dataframe_schema)

    def _write_yaml(obj, stream):
        return yaml.safe_dump(obj, stream=stream, sort_keys=False)

    try:
        with Path(stream).open("w", encoding="utf-8") as f:
            _write_yaml(statistics, f)
    except (TypeError, OSError):
        return _write_yaml(statistics, stream)


SCRIPT_TEMPLATE = """
from pandera import (
    DataFrameSchema, Column, Check, Index, MultiIndex
)

schema = DataFrameSchema(
    columns={{{columns}}},
    index={index},
    coerce={coerce},
    strict={strict},
    name={name},
)
"""

COLUMN_TEMPLATE = """
Column(
    dtype={dtype},
    checks={checks},
    nullable={nullable},
    unique={unique},
    coerce={coerce},
    required={required},
    regex={regex},
)
"""

INDEX_TEMPLATE = (
    "Index(dtype={dtype},checks={checks},"
    "nullable={nullable},coerce={coerce},name={name})"
)

MULTIINDEX_TEMPLATE = """
MultiIndex(indexes=[{indexes}])
"""


def _format_checks(checks_dict):
    if checks_dict is None:
        return "None"

    checks = []
    for check_name, check_kwargs in checks_dict.items():
        if check_kwargs is None:
            warnings.warn(
                f"Check {check_name} cannot be serialized. "
                "This check will be ignored"
            )
        else:
            args = ", ".join(
                f"{k}={v.__repr__()}" for k, v in check_kwargs.items()
            )
            checks.append(f"Check.{check_name}({args})")
    return f"[{', '.join(checks)}]"


def _format_index(index_statistics):
    index = []
    for properties in index_statistics:
        dtype = properties.get("dtype")
        index_code = INDEX_TEMPLATE.format(
            dtype=f"{_get_qualified_name(dtype.__class__)}",
            checks=(
                "None"
                if properties["checks"] is None
                else _format_checks(properties["checks"])
            ),
            nullable=properties["nullable"],
            coerce=properties["coerce"],
            name=(
                "None"
                if properties["name"] is None
                else f"\"{properties['name']}\""
            ),
        )
        index.append(index_code.strip())

    if len(index) == 1:
        return index[0]

    return MULTIINDEX_TEMPLATE.format(indexes=",".join(index)).strip()


def _format_script(script):
    formatter = partial(black.format_str, mode=black.FileMode(line_length=80))
    return formatter(script)


def to_script(dataframe_schema, path_or_buf=None):
    """Write :class:`~pandera.schemas.DataFrameSchema` to a python script.

    :param dataframe_schema: schema to write to file or dump to string.
    :param path_or_buf: filepath or buf stream to write to. If None, outputs
        string representation of the script.
    :returns: yaml string if stream is None, otherwise returns None.
    """
    statistics = get_dataframe_schema_statistics(dataframe_schema)

    columns = {}
    for colname, properties in statistics["columns"].items():
        dtype = properties.get("dtype")
        column_code = COLUMN_TEMPLATE.format(
            dtype=(
                None if dtype is None else _get_qualified_name(dtype.__class__)
            ),
            checks=_format_checks(properties["checks"]),
            nullable=properties["nullable"],
            unique=properties["unique"],
            coerce=properties["coerce"],
            required=properties["required"],
            regex=properties["regex"],
        )
        columns[colname] = column_code.strip()

    index = (
        None
        if statistics["index"] is None
        else _format_index(statistics["index"])
    )

    column_str = ", ".join(f"'{k}': {v}" for k, v in columns.items())

    script = SCRIPT_TEMPLATE.format(
        columns=column_str,
        index=index,
        coerce=dataframe_schema.coerce,
        strict=dataframe_schema.strict,
        name=dataframe_schema.name.__repr__(),
        unique=dataframe_schema.unique,
    ).strip()

    # add pandas imports to handle datetime and timedelta.
    if "Timedelta" in script:
        script = "from pandas import Timedelta\n" + script
    if "Timestamp" in script:
        script = "from pandas import Timestamp\n" + script

    formatted_script = _format_script(script)

    if path_or_buf is None:
        return formatted_script

    with Path(path_or_buf).open("w", encoding="utf-8") as f:
        f.write(formatted_script)


class FrictionlessFieldParser:
    """Parses frictionless data schema field specifications so we can convert
    them to an equivalent Pandera :class:`~pandera.schema_components.Column`
    schema.

    For this implementation, we are using field names, constraints and types
    but leaving other frictionless parameters out (e.g. foreign keys, type
    formats, titles, descriptions).

    :param field: a field object from a frictionless schema.
    :param primary_keys: the primary keys from a frictionless schema. These
        are used to ensure primary key fields are treated properly - no
        duplicates, no missing values etc.
    """

    def __init__(self, field, primary_keys) -> None:
        self.constraints = field.constraints or {}
        self.primary_keys = primary_keys
        self.name = field.name
        self.type = field.get("type", "string")

    @property
    def dtype(self) -> str:
        """Determine what type of field this is, so we can feed that into
        :class:`~pandera.dtypes.DataType`. If no type is specified in the
        frictionless schema, we default to string values.

        :returns: the pandas-compatible representation of this field type as a
            string.
        """
        types = {
            "string": "string",
            "number": "float",
            "integer": "int",
            "boolean": "bool",
            "object": "object",
            "array": "object",
            "date": "string",
            "time": "string",
            "datetime": "datetime64[ns]",
            "year": "int",
            "yearmonth": "string",
            "duration": "timedelta64[ns]",
            "geopoint": "object",
            "geojson": "object",
            "any": "string",
        }
        return (
            "category"
            if self.constraints.get("enum", None)
            else types[self.type]
        )

    @property
    def checks(self) -> Optional[Dict]:
        """Convert a set of frictionless schema field constraints into checks.

        This parses the standard set of frictionless constraints which can be
        found
        `here <https://specs.frictionlessdata.io/table-schema/#constraints>`_
        and maps them into the equivalent pandera checks.

        :returns: a dictionary of pandera :class:`~pandera.checks.Check`
            objects which capture the standard constraint logic of a
            frictionless schema field.
        """
        if not self.constraints:
            return None
        constraints = self.constraints.copy()
        checks = {}

        def _combine_constraints(check_name, min_constraint, max_constraint):
            """Catches bounded constraints where we need to combine a min and max
            pair of constraints into a single check."""
            if min_constraint in constraints and max_constraint in constraints:
                checks[check_name] = {
                    "min_value": constraints.pop(min_constraint),
                    "max_value": constraints.pop(max_constraint),
                }

        _combine_constraints("in_range", "minimum", "maximum")
        _combine_constraints("str_length", "minLength", "maxLength")

        for constraint_type, constraint_value in constraints.items():
            if constraint_type == "maximum":
                checks["less_than_or_equal_to"] = constraint_value
            elif constraint_type == "minimum":
                checks["greater_than_or_equal_to"] = constraint_value
            elif constraint_type == "maxLength":
                checks["str_length"] = {
                    "min_value": None,
                    "max_value": constraint_value,
                }
            elif constraint_type == "minLength":
                checks["str_length"] = {
                    "min_value": constraint_value,
                    "max_value": None,
                }
            elif constraint_type == "pattern":
                checks["str_matches"] = rf"^{constraint_value}$"
            elif constraint_type == "enum":
                checks["isin"] = constraint_value
        return checks or None

    @property
    def nullable(self) -> bool:
        """Determine whether this field can contain missing values.

        If a field is a primary key, this will return ``False``."""
        if self.name in self.primary_keys:
            return False
        return not self.constraints.get("required", False)

    @property
    def unique(self) -> bool:
        """Determine whether this field can contain duplicate values.

        If a field is a primary key, this will return ``True``.
        """

        # only set column-level uniqueness property if `primary_keys` contains
        # more than one field name.
        if len(self.primary_keys) == 1 and self.name in self.primary_keys:
            return True
        return self.constraints.get("unique", False)

    @property
    def coerce(self) -> bool:
        """Determine whether values within this field should be coerced.

        This currently returns ``True`` for all fields within a frictionless
        schema.
        """
        return True

    @property
    def required(self) -> bool:
        """Determine whether this field must exist within the data.

        This currently returns ``True`` for all fields within a frictionless
        schema.
        """
        return True

    @property
    def regex(self) -> bool:
        """Determine whether this field name should be used for regex matches.

        This currently returns ``False`` for all fields within a frictionless
        schema.
        """
        return False

    def to_pandera_column(self) -> Dict:
        """Export this field to a column spec dictionary."""
        return {
            "checks": self.checks,
            "coerce": self.coerce,
            "nullable": self.nullable,
            "unique": self.unique,
            "dtype": self.dtype,
            "required": self.required,
            "name": self.name,
            "regex": self.regex,
        }


def from_frictionless_schema(
    schema: Union[str, Path, Dict, FrictionlessSchema]
) -> DataFrameSchema:
    # pylint: disable=line-too-long
    """Create a :class:`~pandera.schemas.DataFrameSchema` from either a
    frictionless json/yaml schema file saved on disk, or from a frictionless
    schema already loaded into memory.

    Each field from the frictionless schema will be converted to a pandera
    column specification using :class:`~pandera.io.FrictionlessFieldParser`
    to map field characteristics to pandera column specifications.

    :param schema: the frictionless schema object (or a
        string/Path to the location on disk of a schema specification) to
        parse.
    :returns: dataframe schema with frictionless field specs converted to
        pandera column checks and constraints for use as normal.

    :example:

    Here, we're defining a very basic frictionless schema in memory before
    parsing it and then querying the resulting
    :class:`~pandera.schemas.DataFrameSchema` object as per any other Pandera
    schema:

    >>> from pandera.io import from_frictionless_schema
    >>>
    >>> FRICTIONLESS_SCHEMA = {
    ...     "fields": [
    ...         {
    ...             "name": "column_1",
    ...             "type": "integer",
    ...             "constraints": {"minimum": 10, "maximum": 99}
    ...         },
    ...         {
    ...             "name": "column_2",
    ...             "type": "string",
    ...             "constraints": {"maxLength": 10, "pattern": "\\S+"}
    ...         },
    ...     ],
    ...     "primaryKey": "column_1"
    ... }
    >>> schema = from_frictionless_schema(FRICTIONLESS_SCHEMA)
    >>> schema.columns["column_1"].checks
    [<Check in_range: in_range(10, 99)>]
    >>> schema.columns["column_1"].required
    True
    >>> schema.columns["column_1"].unique
    True
    >>> schema.columns["column_2"].checks
    [<Check str_length: str_length(None, 10)>, <Check str_matches: str_matches(re.compile('^\\\\S+$'))>]
    """
    if not isinstance(schema, FrictionlessSchema):
        schema = FrictionlessSchema(schema)

    assembled_schema = {
        "columns": {
            field.name: FrictionlessFieldParser(
                field, schema.primary_key
            ).to_pandera_column()
            for field in schema.fields
        },
        "index": None,
        "checks": None,
        "coerce": True,
        "strict": True,
        # only set dataframe-level uniqueness if the frictionless primary
        # key property specifies more than one field
        "unique": (
            None if len(schema.primary_key) == 1 else list(schema.primary_key)
        ),
    }
    return _deserialize_schema(assembled_schema)
