"""Authenticated identities and repository authorization contracts."""

from .config import CAPABILITIES, TokenRecord, ref_matches, validate_ref_pattern, validate_token_record
from .identity import AuthenticatedPrincipal, AuthManager
from .policy import RefPolicy

__all__ = [
    "AuthenticatedPrincipal",
    "AuthManager",
    "CAPABILITIES",
    "RefPolicy",
    "TokenRecord",
    "ref_matches",
    "validate_ref_pattern",
    "validate_token_record",
]
