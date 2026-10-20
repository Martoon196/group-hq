"""The four product connectors and the registry the recompute job picks them from.

    with open_connectors(dry_run=True) as connectors:
        connectors["pp"].grant(grant, person)

Each connector reads its base URL and token from app.config at construction and never logs either.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from ... import config
from .base import BaseConnector, ConnectorError, ConnectorResult, iso_utc, normalise_email
from .hnh import HNHConnector
from .kp import KPConnector
from .ordris import OrdrisConnector
from .pp import PPConnector

CONNECTORS: dict[str, type[BaseConnector]] = {
    "pp": PPConnector,
    "ordris": OrdrisConnector,
    "kp": KPConnector,
    "hnh": HNHConnector,
}


def get_connector(product: str, *, dry_run: bool = False) -> BaseConnector:
    try:
        cls = CONNECTORS[product]
    except KeyError:
        raise ConnectorError("unsupported", detail=f"no connector for product {product!r}") from None
    return cls.from_config(dry_run=dry_run)


def product_dry_run(product: str, dry_run: bool = False) -> bool:
    """A product is in dry run when the run is, when CONNECTORS_DRY_RUN is on for everything, or when
    CONNECTORS_DRY_RUN names that product (for example ordris,kp,hnh while only PP's endpoints exist)."""
    return bool(dry_run) or bool(config.CONNECTORS_DRY_RUN) or product in config.CONNECTORS_DRY_RUN_PRODUCTS


@contextmanager
def open_connectors(*, dry_run: bool = False) -> Iterator[dict[str, BaseConnector]]:
    """All four, closed together when the block ends. Each one is in dry run when product_dry_run says so."""
    pool = {product: cls.from_config(dry_run=product_dry_run(product, dry_run)) for product, cls in CONNECTORS.items()}
    try:
        yield pool
    finally:
        for connector in pool.values():
            connector.close()


__all__ = ["BaseConnector", "ConnectorError", "ConnectorResult", "CONNECTORS", "HNHConnector", "KPConnector",
           "OrdrisConnector", "PPConnector", "get_connector", "iso_utc", "normalise_email", "open_connectors",
           "product_dry_run"]
