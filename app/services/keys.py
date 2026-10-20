"""The person key, defined once (docs/shared-keys.md, section 1) and used by the login, the register and
every connector, so the rule can never drift between them."""
from __future__ import annotations


def normalise_email(email: str | None) -> str:
    """lower(trim(email)): nothing else, no dot-stripping, no tag removal."""
    return (email or "").strip().lower()
