"""Security tests for JWT verification.

Tests that verify_token() correctly rejects invalid/forged tokens.
Uses real RSA key pairs generated in-process (no Keycloak needed).

Mark: @pytest.mark.auth
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from jose.exceptions import JWTError

from src.core.clients.jwks import verify_token


def generate_rsa_keypair() -> tuple[str, dict[str, Any]]:
    """Return (private_key_pem_str, public_jwk_dict)."""
    from jose.backends import RSAKey
    from jose.constants import ALGORITHMS

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()

    jwk_key = RSAKey(private_key, ALGORITHMS.RS256)
    public_jwk: dict[str, Any] = jwk_key.public_key().to_dict()

    return private_pem, public_jwk


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, dict[str, Any]]:  # type: ignore[misc]
    return generate_rsa_keypair()


@pytest.fixture(autouse=True)
def pin_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin KEYCLOAK_AUDIENCE to 'rag-api' so tests are self-contained."""
    import src.core.clients.jwks as jwks_mod

    mock_settings = MagicMock()
    mock_settings.KEYCLOAK_AUDIENCE = "rag-api"
    monkeypatch.setattr(jwks_mod, "settings", mock_settings)


def make_token(
    private_key_pem: str,
    algorithm: str = "RS256",
    **claims: Any,
) -> str:
    payload: dict[str, Any] = {
        "sub": "kc-user",
        "aud": "rag-api",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "tenant_id": str(uuid.uuid4()),
    }
    payload.update(claims)
    return jwt.encode(payload, private_key_pem, algorithm=algorithm)


@pytest.mark.auth
class TestJWTVerification:
    def test_valid_rs256_token_is_accepted(self, rsa_keypair: tuple[str, dict[str, Any]]) -> None:
        """Token signed with correct RSA key → claims returned."""
        private_pem, public_jwk = rsa_keypair
        token = make_token(private_pem)
        claims = verify_token(token, [public_jwk])
        assert claims["sub"] == "kc-user"
        assert claims["aud"] == "rag-api"

    def test_none_algorithm_rejected(self, rsa_keypair: tuple[str, dict[str, Any]]) -> None:
        """Token with alg=none is rejected when verified against a valid RS256 key."""
        _, public_jwk = rsa_keypair

        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "none", "typ": "JWT"}).encode()
        ).rstrip(b"=")
        payload_data = {
            "sub": "attacker",
            "aud": "rag-api",
            "exp": int(time.time()) + 3600,
        }
        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload_data).encode()
        ).rstrip(b"=")
        none_token = f"{header.decode()}.{payload_b64.decode()}."

        # Must raise JWTError — not a catch-all Exception
        with pytest.raises(JWTError):
            verify_token(none_token, [public_jwk])

    def test_hs256_token_rejected(self, rsa_keypair: tuple[str, dict[str, Any]]) -> None:
        """HS256 token rejected even when verified against a valid RSA public key."""
        _, public_jwk = rsa_keypair
        hs256_token = jwt.encode(
            {"sub": "attacker", "aud": "rag-api", "exp": int(time.time()) + 3600},
            "secret",
            algorithm="HS256",
        )
        with pytest.raises(JWTError):
            verify_token(hs256_token, [public_jwk])

    def test_token_with_wrong_rsa_key_rejected(self) -> None:
        """Token signed with key A is rejected when verified with key B."""
        private_a, _ = generate_rsa_keypair()
        _, public_b = generate_rsa_keypair()

        token = make_token(private_a)
        with pytest.raises(JWTError):
            verify_token(token, [public_b])

    def test_expired_token_rejected(self, rsa_keypair: tuple[str, dict[str, Any]]) -> None:
        """Token with exp in the past → JWTError."""
        private_pem, public_jwk = rsa_keypair
        token = make_token(private_pem, exp=int(time.time()) - 10)
        with pytest.raises(JWTError):
            verify_token(token, [public_jwk])

    def test_wrong_audience_rejected(self, rsa_keypair: tuple[str, dict[str, Any]]) -> None:
        """Token with aud != 'rag-api' → JWTError."""
        private_pem, public_jwk = rsa_keypair
        token = make_token(private_pem, aud="not-rag-api")
        with pytest.raises(JWTError):
            verify_token(token, [public_jwk])

    def test_token_without_aud_rejected(self, rsa_keypair: tuple[str, dict[str, Any]]) -> None:
        """Token missing aud claim → JWTError (explicit post-decode check)."""
        private_pem, public_jwk = rsa_keypair
        payload: dict[str, Any] = {
            "sub": "kc-user",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            # aud intentionally absent
        }
        token = jwt.encode(payload, private_pem, algorithm="RS256")
        with pytest.raises(JWTError):
            verify_token(token, [public_jwk])

    def test_key_rotation_tries_all_keys(self, rsa_keypair: tuple[str, dict[str, Any]]) -> None:
        """When JWKS has multiple keys, iteration finds the correct one."""
        private_pem, public_jwk = rsa_keypair
        _, other_jwk = generate_rsa_keypair()

        token = make_token(private_pem)
        # Wrong key first, then correct key — should succeed
        claims = verify_token(token, [other_jwk, public_jwk])
        assert claims["sub"] == "kc-user"

    def test_future_iat_handled_gracefully(
        self, rsa_keypair: tuple[str, dict[str, Any]]
    ) -> None:
        """Token with iat in the future is accepted — jose does not reject future iat."""
        private_pem, public_jwk = rsa_keypair
        token = make_token(private_pem, iat=int(time.time()) + 60)
        claims = verify_token(token, [public_jwk])
        assert claims["sub"] == "kc-user"
