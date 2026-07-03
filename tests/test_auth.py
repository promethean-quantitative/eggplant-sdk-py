"""L1/L2 auth golden vectors.

The vectors are shared with the Rust sibling SDK (which cross-checked them
against the official client) — an exact signature match pins the whole
ClobAuth EIP-712 stack and the deterministic ECDSA signer.
"""

import uuid

from eggplant_sdk.auth import (
    POLY_ADDRESS,
    POLY_API_KEY,
    POLY_NONCE,
    POLY_PASSPHRASE,
    POLY_SIGNATURE,
    POLY_TIMESTAMP,
    Credentials,
    l1_headers,
    l2_headers,
    l2_hmac,
    l2_message,
)
from eggplant_sdk.chain import AMOY
from eggplant_sdk.signer import LocalSigner

# Publicly known throwaway key (the anvil dev key).
PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def test_l1_headers_match_golden_vector():
    signer = LocalSigner(PRIVATE_KEY)
    headers = l1_headers(signer, AMOY, 10_000_000, 23)

    assert headers[POLY_ADDRESS] == "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
    assert headers[POLY_NONCE] == "23"
    assert headers[POLY_SIGNATURE] == (
        "0xf62319a987514da40e57e2f4d7529f7bac38f0355bd88bb5adbb3768d80de6c1"
        "682518e0af677d5260366425f4361e7b70c25ae232aff0ab2331e2b164a1aedc1b"
    )
    assert headers[POLY_TIMESTAMP] == "10000000"


def test_l2_headers_match_golden_vector():
    signer = LocalSigner(PRIVATE_KEY)
    credentials = Credentials(
        uuid.UUID(int=0),
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )

    headers = l2_headers(signer.address, credentials, 1, "GET", "/", "")

    assert headers[POLY_ADDRESS] == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    assert headers[POLY_PASSPHRASE] == "a" * 64
    assert headers[POLY_API_KEY] == str(uuid.UUID(int=0))
    assert headers[POLY_SIGNATURE] == "eHaylCwqRSOa2LFD77Nt_SaTpbsxzN8eTEI3LryhEj4="
    assert headers[POLY_TIMESTAMP] == "1"


def test_l2_message_layout():
    assert l2_message(1, "POST", "/path", '{"foo":"bar"}') == '1POST/path{"foo":"bar"}'


def test_l2_hmac_matches_golden_vector():
    message = l2_message(1_000_000, "test-sign", "/orders", '{"hash":"0x123"}')
    assert message == '1000000test-sign/orders{"hash":"0x123"}'

    signature = l2_hmac("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", message)
    assert signature == "4gJVbox-R6XlDK4nlaicig0_ANVL1qdcahiL8CXfXLM="


def test_repr_does_not_expose_secrets():
    secret_value = "my_super_secret_value_12345"
    passphrase_value = "my_super_secret_passphrase_67890"
    credentials = Credentials(uuid.UUID(int=0), secret_value, passphrase_value)

    rendered = repr(credentials)
    assert secret_value not in rendered
    assert passphrase_value not in rendered


def test_credentials_parse_from_venue_shape():
    creds = Credentials.from_dict(
        {
            "apiKey": "019097a4-cb4e-79d8-bb5f-b4f8b1d800f5",
            "secret": "c2VjcmV0",
            "passphrase": "pass",
        }
    )
    assert str(creds.key) == "019097a4-cb4e-79d8-bb5f-b4f8b1d800f5"
    assert creds.secret() == "c2VjcmV0"
    assert creds.passphrase() == "pass"
