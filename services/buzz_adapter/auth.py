"""Per-user Buzz token authentication and scoped AgentOS JWT issuance."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

ROUTER_SCOPE = "teams:workforce-router:run"
WORKSPACE_APPROVAL_PURPOSE = "workspace_patch_approval"


def token_digest(token: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), token.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class BuzzIdentity:
    subject: str
    token_hash: str
    active: bool = True

    def __post_init__(self) -> None:
        if not self.subject.startswith("buzz:") or len(self.subject) <= len("buzz:"):
            raise ValueError("Buzz subjects must use the buzz:<user-id> namespace")


class BuzzIdentityStore:
    def __init__(self, identities: tuple[BuzzIdentity, ...], pepper: str):
        self._identities = identities
        self._pepper = pepper

    def authenticate(self, token: str) -> BuzzIdentity | None:
        supplied = token_digest(token, self._pepper)
        for identity in self._identities:
            if identity.active and hmac.compare_digest(supplied, identity.token_hash):
                return identity
        return None


class ScopedJwtIssuer:
    def __init__(
        self,
        private_key: Any,
        audience: str,
        issuer: str = "buzz-adapter",
        lifetime: timedelta = timedelta(seconds=60),
    ):
        self.private_key = private_key
        self.audience = audience
        self.issuer = issuer
        self.lifetime = lifetime

    def issue(self, subject: str) -> str:
        now = datetime.now(UTC)
        claims = {
            "sub": subject,
            "aud": self.audience,
            "iss": self.issuer,
            "iat": now,
            "nbf": now,
            "exp": now + self.lifetime,
            "jti": secrets.token_hex(16),
            "scopes": [ROUTER_SCOPE],
        }
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    def issue_workspace_approval(
        self,
        *,
        subject: str,
        session_id: str,
        workspace_id: str,
        patch_text: str,
        audience: str,
    ) -> str:
        """Sign one short-lived approval for one exact workspace patch."""
        now = datetime.now(UTC)
        claims = {
            "sub": subject,
            "aud": audience,
            "iss": self.issuer,
            "iat": now,
            "nbf": now,
            "exp": now + self.lifetime,
            "jti": secrets.token_hex(16),
            "purpose": WORKSPACE_APPROVAL_PURPOSE,
            "session_id": session_id,
            "workspace_id": workspace_id,
            "patch_sha256": hashlib.sha256(patch_text.encode()).hexdigest(),
        }
        return jwt.encode(claims, self.private_key, algorithm="RS256")
