"""Authenticated principals, credential reload, and explicit capability checks."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import threading

from ..store import ScionError, validate_ref
from .config import CAPABILITIES, file_signature, load_auth_config, ref_matches


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    id: str
    capabilities: frozenset[str]
    refs: tuple[str, ...]

    def require(self, capability: str, ref: str | None = None) -> None:
        """Admin is an independent capability and never bypasses write/ref scopes."""
        if ref is not None:
            validate_ref(ref)
        if capability not in self.capabilities:
            raise ScionError("Credential does not grant the required capability", "forbidden", 403)
        if ref is not None and not any(ref_matches(pattern, ref) for pattern in self.refs):
            raise ScionError("Credential does not grant access to this ref", "forbidden", 403)


class AuthManager:
    """Authenticate with hashed config records, a legacy token, or trusted local mode."""

    def __init__(self, token: str | None = None, auth_file: str | Path | None = None):
        if token is not None and auth_file is not None:
            raise ScionError("A legacy token and an authentication file cannot be combined")
        if token is not None and (not isinstance(token, str) or not token or len(token) > 4096
                                  or any(not "!" <= char <= "~" for char in token)):
            raise ScionError("Token must be a nonempty printable ASCII string without whitespace")
        if auth_file is not None and (not isinstance(auth_file, (str, Path)) or not str(auth_file)):
            raise ScionError("Authentication file must be a nonempty path")
        self.auth_file = Path(auth_file).expanduser().absolute() if auth_file is not None else None
        self.enabled = token is not None or self.auth_file is not None
        self._legacy_hash = hashlib.sha256(token.encode("utf-8")).hexdigest() if token is not None else None
        self._lock = threading.Lock()
        self._signature = None
        self._records = ()
        if self.auth_file is not None:
            with self._lock:
                self._refresh()

    def _refresh(self) -> None:
        try:
            signature = file_signature(self.auth_file)
            if signature != self._signature:
                signature, records = load_auth_config(self.auth_file)
                self._records = records
                self._signature = signature
        except (OSError, ValueError, ScionError, RecursionError):
            self._records = ()
            self._signature = None
            raise ScionError("Authentication configuration is unavailable or invalid", "auth_config", 503) from None

    def configuration_ready(self) -> bool:
        """Fail readiness when a configured credential generation cannot be loaded."""
        if self.auth_file is None:
            return True
        with self._lock:
            try:
                self._refresh()
            except ScionError:
                return False
        return True

    @staticmethod
    def _credential(authorization: str | None) -> str:
        if not isinstance(authorization, str):
            raise ScionError("A valid bearer token is required", "unauthorized", 401)
        scheme, separator, token = authorization.partition(" ")
        if (not separator or scheme.lower() != "bearer" or not token or len(token) > 4096
                or any(not "!" <= char <= "~" for char in token)):
            raise ScionError("A valid bearer token is required", "unauthorized", 401)
        return token

    def authenticate(self, authorization: str | None) -> AuthenticatedPrincipal:
        if not self.enabled:
            return AuthenticatedPrincipal("local", CAPABILITIES, ("*",))
        with self._lock:
            if self.auth_file is not None:
                self._refresh()
            token = self._credential(authorization)
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if self._legacy_hash is not None and hmac.compare_digest(digest, self._legacy_hash):
                return AuthenticatedPrincipal("legacy", CAPABILITIES, ("*",))
            matched = None
            for record in self._records:
                if hmac.compare_digest(digest, record.sha256) and not record.revoked:
                    matched = record
            if matched is not None:
                return AuthenticatedPrincipal(matched.id, matched.capabilities, matched.refs)
        raise ScionError("A valid bearer token is required", "unauthorized", 401)

    @staticmethod
    def require(principal: AuthenticatedPrincipal, capability: str, ref: str | None = None) -> None:
        if not isinstance(principal, AuthenticatedPrincipal):
            raise ScionError("A valid bearer token is required", "unauthorized", 401)
        principal.require(capability, ref)
