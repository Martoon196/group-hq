"""The four product connectors and the registry the recompute job picks them from.

    with open_connectors(dry_run=True, switches=settings.product_switches(db)) as connectors:
        connectors["pp"].grant(grant, person)

Each connector reads its base URL and token from app.config at construction and never logs either. The
product switches (Live / Dry run on the Settings screen) are read from the database by the caller and passed
in as `switches`, so this module never touches the database.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Mapping

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


SWITCH_MODES = ("live", "dry_run")


def _switched(product: str, switches: Mapping[str, str] | None) -> str:
    value = (switches or {}).get(product, "")
    return value if value in SWITCH_MODES else ""


def product_dry_run(product: str, dry_run: bool = False, *, switches: Mapping[str, str] | None = None) -> bool:
    """Is the product in dry run? In order: the run itself is a dry run; CONNECTORS_DRY_RUN=1 holds every product
    in dry run (a hard floor no switch can lift); the product's switch on the Settings screen, once it has been
    set (`switches`, read by the caller from settings.product_switches); otherwise the environment default,
    which is whether CONNECTORS_DRY_RUN names the product (for example "pp" while PP clears its audit)."""
    if dry_run or config.CONNECTORS_DRY_RUN:
        return True
    switched = _switched(product, switches)
    if switched:
        return switched == "dry_run"
    return product in config.CONNECTORS_DRY_RUN_PRODUCTS


def product_mode(product: str, *, switches: Mapping[str, str] | None = None) -> dict:
    """How the product is treated now, for the screens. state is dry_run, live, or not_configured (live by the
    rule, but its token or base URL is missing, so nothing can be sent whatever the switch says); source is
    floor (CONNECTORS_DRY_RUN=1), switch or environment; switch is the stored switch or ""."""
    dry = product_dry_run(product, switches=switches)
    configured = CONNECTORS[product].from_config(dry_run=dry).configured   # no client is opened until a request
    if config.CONNECTORS_DRY_RUN:
        source = "floor"
    elif _switched(product, switches):
        source = "switch"
    else:
        source = "environment"
    state = "dry_run" if dry else ("live" if configured else "not_configured")
    return {"product": product, "state": state, "dry_run": dry, "configured": configured, "source": source,
            "switch": _switched(product, switches)}


@contextmanager
def open_connectors(*, dry_run: bool = False, switches: Mapping[str, str] | None = None) -> Iterator[dict[str, BaseConnector]]:
    """All four, closed together when the block ends. Each one is in dry run when product_dry_run says so; pass the
    product switches (settings.product_switches(db)) or only the environment decides."""
    pool = {product: cls.from_config(dry_run=product_dry_run(product, dry_run, switches=switches))
            for product, cls in CONNECTORS.items()}
    try:
        yield pool
    finally:
        for connector in pool.values():
            connector.close()


__all__ = ["BaseConnector", "ConnectorError", "ConnectorResult", "CONNECTORS", "HNHConnector", "KPConnector",
           "OrdrisConnector", "PPConnector", "SWITCH_MODES", "get_connector", "iso_utc", "normalise_email",
           "open_connectors", "product_dry_run", "product_mode"]
