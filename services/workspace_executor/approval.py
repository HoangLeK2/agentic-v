"""Verification for one-time Buzz workspace patch approvals."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import jwt

from services.buzz_adapter.auth import WORKSPACE_APPROVAL_PURPOSE


@dataclass(frozen=True)
class WorkspaceApproval:
    nonce: str
    expires_at: int
    subject: str
    session_id: str


class WorkspaceApprovalVerifier:
    def __init__(self, public_key_file: Path, audience: str, issuer: str = "buzz-adapter"):
        self._public_key = public_key_file.read_bytes()
        self._audience = audience
        self._issuer = issuer

    def verify(self, token: str, *, workspace_id: str, patch_text: str) -> WorkspaceApproval:
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": [
                        "sub",
                        "aud",
                        "iss",
                        "iat",
                        "nbf",
                        "exp",
                        "jti",
                        "purpose",
                        "session_id",
                        "workspace_id",
                        "patch_sha256",
                    ]
                },
            )
        except jwt.PyJWTError as exc:
            raise PermissionError("Workspace patch approval is invalid or expired") from exc

        expected_digest = hashlib.sha256(patch_text.encode()).hexdigest()
        if claims["purpose"] != WORKSPACE_APPROVAL_PURPOSE:
            raise PermissionError("Workspace patch approval has the wrong purpose")
        if claims["workspace_id"] != workspace_id:
            raise PermissionError("Workspace patch approval does not match this workspace")
        if claims["patch_sha256"] != expected_digest:
            raise PermissionError("Workspace patch approval does not match this patch")

        nonce = claims["jti"]
        subject = claims["sub"]
        session_id = claims["session_id"]
        expires_at = claims["exp"]
        if not all(isinstance(value, str) and value for value in (nonce, subject, session_id)):
            raise PermissionError("Workspace patch approval has invalid identity claims")
        if not isinstance(expires_at, int):
            raise PermissionError("Workspace patch approval has an invalid expiry")
        return WorkspaceApproval(
            nonce=nonce,
            expires_at=expires_at,
            subject=subject,
            session_id=session_id,
        )
