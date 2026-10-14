import base64
from unittest import mock

import pytest
import requests

from script import KEY_CONFIG_URL, InvalidKeyConfig, get_source_records, main, parse_key_config


# Real response captured from the Google endpoint on 2026-09-14: a single bare
# OHTTP Key Config (RFC 9458 Section 3.1), 41 bytes long.
SAMPLE_KEY_CONFIG = base64.b64decode("HQAgLPp299yewWibKwPaxVRye21WjLCpEU75A0vrhBqgqyoABAABAAI=")

# The Remote Settings record built from SAMPLE_KEY_CONFIG.
LATEST_RECORD = {
    "id": "latest",
    "key_config": "HQAgLPp299yewWibKwPaxVRye21WjLCpEU75A0vrhBqgqyoABAABAAI=",
    "key_id": 29,
}


@pytest.fixture
def mocked_client():
    # Mock the kinto_http.Client where it is imported.
    with mock.patch("script.kinto_http.Client", spec=True) as mocked_class:
        yield mocked_class()


@pytest.fixture
def mocked_get():
    # Mock requests where it is imported, returning a valid key config by default.
    with mock.patch("script.requests.get") as mock_get:
        response = mock_get.return_value
        response.content = SAMPLE_KEY_CONFIG
        response.headers = {"Content-Type": "application/ohttp-keys"}
        yield mock_get


def test_cannot_call_unknown_method(mocked_client):
    # Thanks to the spec=True argument, the mock will raise an
    # AttributeError if we try to call a method that doesn't exist
    with pytest.raises(AttributeError):
        mocked_client.unknown_method()


def test_main_anonymous(mocked_client, mocked_get, capsys):
    mocked_client.server_info.return_value = {}

    main()

    mocked_client.server_info.assert_called_once()
    assert "⚠️ Anonymous" in capsys.readouterr().out


def test_main_logged_in(mocked_client, mocked_get, capsys):
    mocked_client.server_info.return_value = {"user": {"id": "account:bot"}}

    main()

    mocked_client.server_info.assert_called_once()
    assert "Logged in as account:bot" in capsys.readouterr().out


def test_get_source_records_builds_latest_record(mocked_get):
    records = get_source_records()

    assert records == [LATEST_RECORD]
    mocked_get.assert_called_once()
    assert mocked_get.call_args.args == (KEY_CONFIG_URL,)
    assert mocked_get.call_args.kwargs.get("timeout")


def test_get_source_records_propagates_http_errors(mocked_get):
    mocked_get.return_value.raise_for_status.side_effect = requests.HTTPError("503")

    with pytest.raises(requests.HTTPError):
        get_source_records()


def test_get_source_records_rejects_unexpected_content_type(mocked_get):
    mocked_get.return_value.headers = {"Content-Type": "text/html; charset=utf-8"}
    mocked_get.return_value.content = b"<html>Service Unavailable</html>"

    with pytest.raises(InvalidKeyConfig):
        get_source_records()


def test_get_source_records_rejects_malformed_key_config(mocked_get):
    mocked_get.return_value.content = SAMPLE_KEY_CONFIG[:-1]

    with pytest.raises(InvalidKeyConfig):
        get_source_records()


def test_up_to_date(mocked_client, mocked_get, capsys):
    mocked_client.get_records.return_value = [
        {**LATEST_RECORD, "last_modified": 1700000000000, "schema": 1699999999999},
    ]

    main()

    mocked_client.get_records.assert_called_once()
    mocked_get.assert_called_once()
    assert "Records are in sync" in capsys.readouterr().out
    mocked_client.batch.assert_not_called()


def test_first_run_creates_record(mocked_client, mocked_get, capsys):
    mocked_client.get_records.return_value = []
    batch_client = mocked_client.batch().__enter__()
    batch_client.results.return_value = [LATEST_RECORD]

    main()

    assert "1 operations ✅" in capsys.readouterr().out
    batch_client.create_record.assert_called_once_with(data=LATEST_RECORD)
    batch_client.update_record.assert_not_called()
    batch_client.delete_record.assert_not_called()
    mocked_client.request_review.assert_called_once_with(message="r?")


def test_rotated_key_updates_record_in_place(mocked_client, mocked_get, capsys):
    # Same layout as the sample, but with the previous key id.
    previous_key = bytes([28]) + SAMPLE_KEY_CONFIG[1:]
    mocked_client.get_records.return_value = [
        {
            "id": "latest",
            "key_config": base64.b64encode(previous_key).decode(),
            "key_id": 28,
            "last_modified": 1700000000000,
        },
    ]
    batch_client = mocked_client.batch().__enter__()
    batch_client.results.return_value = [LATEST_RECORD]

    main()

    assert "1 operations ✅" in capsys.readouterr().out
    batch_client.update_record.assert_called_once_with(data=LATEST_RECORD)
    batch_client.create_record.assert_not_called()
    batch_client.delete_record.assert_not_called()
    mocked_client.request_review.assert_called_once_with(message="r?")


def test_stray_record_is_deleted(mocked_client, mocked_get, capsys):
    mocked_client.get_records.return_value = [
        {**LATEST_RECORD, "last_modified": 1700000000000},
        {"id": "stray", "key_config": "AAAA", "key_id": 0, "last_modified": 1700000000001},
    ]
    batch_client = mocked_client.batch().__enter__()
    batch_client.results.return_value = [{"id": "stray", "deleted": True}]

    main()

    assert "1 operations ✅" in capsys.readouterr().out
    batch_client.delete_record.assert_called_once_with(id="stray")
    batch_client.create_record.assert_not_called()
    batch_client.update_record.assert_not_called()


def test_dev_self_approves_changes(mocked_client, mocked_get):
    mocked_client.get_records.return_value = []
    batch_client = mocked_client.batch().__enter__()
    batch_client.results.return_value = [LATEST_RECORD]

    with mock.patch("script.ENVIRONMENT", "dev"):
        main()

    mocked_client.approve_changes.assert_called_once_with()
    mocked_client.request_review.assert_not_called()


def test_parse_key_config_returns_fields_of_valid_config():
    parsed = parse_key_config(SAMPLE_KEY_CONFIG)

    assert parsed == {
        "key_id": 29,
        "kem_id": 0x0020,
        "public_key": bytes.fromhex(
            "2cfa76f7dc9ec1689b2b03dac554727b6d568cb0a9114ef9034beb841aa0ab2a"
        ),
        "symmetric_algorithms": [(0x0001, 0x0002)],
    }


def _with_kem(config, kem_id):
    return config[:1] + kem_id.to_bytes(2, "big") + config[3:]


def _with_algs_length(config, length):
    # The sample uses X25519, so the length field sits at offset 1 + 2 + 32.
    return config[:35] + length.to_bytes(2, "big") + config[37:]


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(SAMPLE_KEY_CONFIG[:-1], id="truncated"),
        pytest.param(SAMPLE_KEY_CONFIG + b"\x00", id="trailing-bytes"),
        pytest.param(_with_kem(SAMPLE_KEY_CONFIG, 0x1234), id="unknown-kem"),
        pytest.param(_with_algs_length(SAMPLE_KEY_CONFIG, 0), id="zero-algs-length"),
        pytest.param(_with_algs_length(SAMPLE_KEY_CONFIG, 3), id="algs-length-not-multiple-of-4"),
    ],
)
def test_parse_key_config_rejects_malformed_input(data):
    with pytest.raises(InvalidKeyConfig):
        parse_key_config(data)
