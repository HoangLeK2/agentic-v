import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import TestCase

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.buzz_adapter.auth import BuzzIdentity, BuzzIdentityStore, ScopedJwtIssuer, token_digest
from services.workspace_executor.approval import WorkspaceApprovalVerifier


class BuzzAuthTest(TestCase):
    private_key: rsa.RSAPrivateKey
    public_key: rsa.RSAPublicKey

    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def test_distinct_bearer_tokens_map_to_distinct_subjects(self) -> None:
        store = BuzzIdentityStore(
            identities=(
                BuzzIdentity(subject="buzz:alice", token_hash=token_digest("alice-token", "pepper")),
                BuzzIdentity(subject="buzz:bob", token_hash=token_digest("bob-token", "pepper")),
            ),
            pepper="pepper",
        )

        alice = store.authenticate("alice-token")
        bob = store.authenticate("bob-token")
        self.assertIsNotNone(alice)
        self.assertIsNotNone(bob)
        assert alice is not None
        assert bob is not None
        self.assertEqual(alice.subject, "buzz:alice")
        self.assertEqual(bob.subject, "buzz:bob")
        self.assertIsNone(store.authenticate("wrong-token"))

    def test_revoked_identity_is_rejected(self) -> None:
        store = BuzzIdentityStore(
            identities=(
                BuzzIdentity(
                    subject="buzz:alice",
                    token_hash=token_digest("alice-token", "pepper"),
                    active=False,
                ),
            ),
            pepper="pepper",
        )

        self.assertIsNone(store.authenticate("alice-token"))

    def test_scoped_jwt_contains_only_router_run_permission(self) -> None:
        issuer = ScopedJwtIssuer(
            private_key=self.private_key,
            audience="agentos",
            issuer="buzz-adapter",
            lifetime=timedelta(seconds=60),
        )

        token = issuer.issue("buzz:alice")
        claims = jwt.decode(
            token,
            self.public_key,
            algorithms=["RS256"],
            audience="agentos",
            issuer="buzz-adapter",
        )

        self.assertEqual(claims["sub"], "buzz:alice")
        self.assertEqual(claims["scopes"], ["teams:workforce-router:run"])
        self.assertIn("jti", claims)

    def test_workspace_approval_is_bound_to_exact_workspace_and_patch(self) -> None:
        issuer = ScopedJwtIssuer(
            private_key=self.private_key,
            audience="agentos",
            lifetime=timedelta(seconds=60),
        )
        token = issuer.issue_workspace_approval(
            subject="buzz:alice",
            session_id="buzz-session",
            workspace_id="workspace123",
            patch_text="approved patch",
            audience="workspace-executor",
        )
        with tempfile.TemporaryDirectory() as directory:
            public_key_file = Path(directory) / "public.pem"
            public_key_file.write_bytes(
                self.public_key.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            verifier = WorkspaceApprovalVerifier(public_key_file, audience="workspace-executor")

            approval = verifier.verify(
                token,
                workspace_id="workspace123",
                patch_text="approved patch",
            )
            self.assertEqual(approval.subject, "buzz:alice")
            self.assertEqual(approval.session_id, "buzz-session")
            with self.assertRaisesRegex(PermissionError, "does not match this patch"):
                verifier.verify(token, workspace_id="workspace123", patch_text="different patch")
            with self.assertRaisesRegex(PermissionError, "does not match this workspace"):
                verifier.verify(token, workspace_id="other", patch_text="approved patch")
