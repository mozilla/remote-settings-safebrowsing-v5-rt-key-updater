import base64
import os
import sys

import kinto_http
import requests
import sentry_sdk
from kinto_http.utils import collection_diff
from sentry_sdk.integrations.gcp import GcpIntegration


AUTHORIZATION = os.getenv("AUTHORIZATION", "")
ENVIRONMENT = os.getenv("ENVIRONMENT", "local").lower()
if ENVIRONMENT not in {"local", "dev", "stage", "prod"}:
    raise ValueError(f"'ENVIRONMENT={ENVIRONMENT}' is not a valid value")
SERVER = os.getenv(
    "SERVER",
    {
        "local": "http://localhost:8888/v1",
        "dev": "https://remote-settings-dev.allizom.org/v1",
        "stage": "https://remote-settings.allizom.org/v1",
        "prod": "https://remote-settings.mozilla.org/v1",
    }[ENVIRONMENT],
)
IS_DRY_RUN = os.getenv("DRY_RUN", "0") in ("1", "y", "Y")
SENTRY_DSN = os.getenv("SENTRY_DSN")
SENTRY_ENV = os.getenv("SENTRY_ENV", ENVIRONMENT)


COLLECTION_NAME = "sbv5-rt-key"
# Google endpoint serving the OHTTP key config of the SafeBrowsing V5 real-time gateway.
KEY_CONFIG_URL = "https://safebrowsingohttpgateway.googleapis.com/v1/ohttp/hpkekeyconfig"
KEY_CONFIG_CONTENT_TYPE = "application/ohttp-keys"
REQUEST_TIMEOUT_SECONDS = 30
# The collection holds a single record, updated in place when Google rotates the key.
RECORD_ID = "latest"

# Public key length (Npk) per HPKE KEM ID, see RFC 9180 Section 7.1.
KEM_PUBLIC_KEY_LENGTHS = {
    0x0010: 65,  # DHKEM(P-256, HKDF-SHA256)
    0x0011: 97,  # DHKEM(P-384, HKDF-SHA384)
    0x0012: 133,  # DHKEM(P-521, HKDF-SHA512)
    0x0020: 32,  # DHKEM(X25519, HKDF-SHA256)
    0x0021: 56,  # DHKEM(X448, HKDF-SHA512)
}


class InvalidKeyConfig(ValueError):
    """Raised when the fetched bytes are not a valid OHTTP Key Config."""


def parse_key_config(data):
    """
    Parse a single OHTTP Key Config (RFC 9458 Section 3.1).

    Key Config {
      Key Identifier (8),
      HPKE KEM ID (16),
      HPKE Public Key (Npk * 8),
      HPKE Symmetric Algorithms Length (16) = 4..65532,
      HPKE Symmetric Algorithms (32) ...,
    }

    Raise ``InvalidKeyConfig`` if the bytes do not encode exactly one key config.
    """
    if len(data) < 3:
        raise InvalidKeyConfig(f"too short ({len(data)} bytes) to hold a key id and KEM id")
    key_id = data[0]
    kem_id = int.from_bytes(data[1:3], "big")
    try:
        npk = KEM_PUBLIC_KEY_LENGTHS[kem_id]
    except KeyError:
        raise InvalidKeyConfig(f"unknown HPKE KEM id 0x{kem_id:04x}") from None

    offset = 3
    public_key = data[offset : offset + npk]
    offset += npk
    if len(data) < offset + 2:
        raise InvalidKeyConfig("truncated before the symmetric algorithms length")
    algs_length = int.from_bytes(data[offset : offset + 2], "big")
    offset += 2
    if algs_length < 4 or algs_length % 4 != 0:
        raise InvalidKeyConfig(f"invalid symmetric algorithms length {algs_length}")
    algs = data[offset : offset + algs_length]
    offset += algs_length
    if len(data) != offset:
        raise InvalidKeyConfig(f"expected exactly {offset} bytes, got {len(data)}")

    symmetric_algorithms = [
        (int.from_bytes(algs[i : i + 2], "big"), int.from_bytes(algs[i + 2 : i + 4], "big"))
        for i in range(0, algs_length, 4)
    ]
    return {
        "key_id": key_id,
        "kem_id": kem_id,
        "public_key": public_key,
        "symmetric_algorithms": symmetric_algorithms,
    }


def fetch_key_config():
    """
    Download the current key config from Google and return its raw bytes.

    Note: despite the ``application/ohttp-keys`` content type, the body is a single
    bare key config (RFC 9458 Section 3.1) without the length prefix that the media
    type mandates (Section 3.2). This is what Firefox's OHTTP client consumes.
    """
    resp = requests.get(KEY_CONFIG_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
    if content_type != KEY_CONFIG_CONTENT_TYPE:
        raise InvalidKeyConfig(
            f"unexpected content type {content_type!r}, expected {KEY_CONFIG_CONTENT_TYPE!r}"
        )
    return resp.content


def get_source_records():
    """
    Turn the key config served by Google into the single Remote Settings record.

    The raw bytes are validated and published verbatim (base64 encoded), so the
    client can decode them and hand them straight to its OHTTP implementation.
    The parsed key id is included to make review diffs readable.
    """
    raw = fetch_key_config()
    parsed = parse_key_config(raw)
    return [
        {
            "id": RECORD_ID,
            "key_config": base64.b64encode(raw).decode(),
            "key_id": parsed["key_id"],
        }
    ]


def main():
    if SENTRY_DSN:
        # Initialize Sentry for error reporting.
        sentry_sdk.init(SENTRY_DSN, integrations=[GcpIntegration()], environment=SENTRY_ENV)
    else:
        print("⚠️ Sentry is not configured. Set SENTRY_DSN environment variable to enable it.")

    client = kinto_http.Client(
        server_url=SERVER,
        auth=AUTHORIZATION,
        bucket="main-workspace",
        collection=COLLECTION_NAME,
        dry_mode=IS_DRY_RUN,
    )

    # Check credentials.
    print("Fetch server info...", end="")
    server_info = client.server_info()
    print("✅")
    if "user" not in server_info:
        print("⚠️ Anonymous")
    else:
        print(f"Logged in as {server_info['user']['id']}")

    # Get source records.
    print("Fetch records from source of truth...", end="")
    source_records = get_source_records()
    print("✅")

    # Get current destination records.
    print("Fetch current destination records...", end="")
    dest_records = client.get_records()
    print("✅")

    # Create or update the destination records.
    to_create, to_update, to_delete = collection_diff(source_records, dest_records)

    has_pending_changes = to_create or to_update or to_delete
    if not has_pending_changes:
        print("Records are in sync. Nothing to do ✅.")
        return os.EX_OK

    # Use a batch request for all operations.
    # Note, if you need to add attachments on records, use individual calls.
    # See https://github.com/Kinto/kinto-http.py/?tab=readme-ov-file#attachments
    print("Apply changes...", end="")
    with client.batch() as batch:
        for r in to_create:
            batch.create_record(data=r)
        for old, new in to_update:
            # Let the server assign a new timestamp.
            new.pop("last_modified", None)
            batch.update_record(data=new)
        for r in to_delete:
            batch.delete_record(id=r["id"])
    ops_count = len(batch.results())
    print(f"{ops_count} operations ✅")

    if ENVIRONMENT == "dev":
        # Self approve changes on DEV.
        client.approve_changes()
    else:
        # Request review.
        print("Request review...", end="")
        client.request_review(message="r?")
        print("✅")

    return os.EX_OK


if __name__ == "__main__":
    sys.exit(main())
