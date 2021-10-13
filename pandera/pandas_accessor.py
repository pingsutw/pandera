"""Register pandas accessor for pandera schema metadata."""

from typing import Optional, Union

import pandas as pd

from . import schemas

Schemas = Union[schemas.DataFrameSchema, schemas.SeriesSchema]


class PanderaAccessor:
    """Pandera accessor for pandas object."""

    def __init__(self, pandas_obj):
        """Initialize the pandera accessor."""
        self._pandas_obj = pandas_obj
        self._schema: Optional[Schemas] = None

    @staticmethod
    def check_schema_type(schema: Schemas):
        """Abstract method for checking the schema type."""
        raise NotImplementedError

    def add_schema(self, schema):
        """Add a schema to the pandas object."""
        self.check_schema_type(schema)
        self._schema = schema
        return self._pandas_obj

    @property
    def schema(self) -> Optional[Schemas]:
        """Access schema metadata."""
        return self._schema

    def validate(self, **kwargs):
        """A utility to validate a dataframe."""
        return self.schema(self._pandas_obj, **kwargs)


@pd.api.extensions.register_dataframe_accessor("pandera")
class PanderaDataFrameAccessor(PanderaAccessor):
    """Pandera accessor for pandas DataFrame."""

    @staticmethod
    def check_schema_type(schema):
        if not isinstance(schema, schemas.DataFrameSchema):
            raise TypeError(
                f"schema arg must be a DataFrameSchema, found {type(schema)}"
            )


@pd.api.extensions.register_series_accessor("pandera")
class PanderaSeriesAccessor(PanderaAccessor):
    """Pandera accessor for pandas Series."""

    @staticmethod
    def check_schema_type(schema):
        if not isinstance(schema, schemas.SeriesSchema):
            raise TypeError(
                f"schema arg must be a SeriesSchema, found {type(schema)}"
            )
