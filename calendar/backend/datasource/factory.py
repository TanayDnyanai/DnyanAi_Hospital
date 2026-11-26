"""Data source factory.

The only place in the codebase that decides *which* storage implementation
is active. Swap ``DATA_SOURCE=production`` once the production adapter
exists; nothing else changes.
"""

from __future__ import annotations

from .. import config
from ..domain import DataSourceError
from .base import DataSource

_instance: DataSource | None = None


def build_data_source() -> DataSource:
    if config.DATA_SOURCE == "legacy":
        from .legacy import LegacyDataSource

        return LegacyDataSource()

    if config.DATA_SOURCE == "production":
        from .production import ProductionDataSource

        return ProductionDataSource()

    if config.DATA_SOURCE == "neon":
        from .postgres import PostgresDataSource

        return PostgresDataSource()

    raise DataSourceError(
        "Unknown DATA_SOURCE "
        f"'{config.DATA_SOURCE}'. Expected 'legacy', 'production' or 'neon'.",
        code="bad_config",
    )


def get_data_source() -> DataSource:
    global _instance
    if _instance is None:
        _instance = build_data_source()
    return _instance


def set_data_source(source: DataSource) -> None:
    """Used by tests and by future runtime configuration."""
    global _instance
    _instance = source


def reset_data_source() -> None:
    global _instance
    _instance = None
