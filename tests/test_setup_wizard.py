import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "tools" / "setup_wizard.py"
SPEC = importlib.util.spec_from_file_location("setup_wizard", MODULE_PATH)
assert SPEC and SPEC.loader
setup_wizard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_wizard)

EXPECTED_SPREADSHEET_ID = setup_wizard.EXPECTED_SPREADSHEET_ID
MAX_BODY_BYTES = setup_wizard.MAX_BODY_BYTES
WizardState = setup_wizard.WizardState
atomic_write_json = setup_wizard.atomic_write_json
build_parser = setup_wizard.build_parser
render_wizard = setup_wizard.render_wizard
security_headers = setup_wizard.security_headers
validate_payload = setup_wizard.validate_payload


def valid_payload():
    return {
        "vless_reality_url": "vless://11111111-1111-1111-1111-111111111111@example.test:443?security=reality",
        "google_service_account": {
            "type": "service_account",
            "client_email": "bot@example.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
        },
        "telegram_bot_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        "deepseek_api_key": "not-a-real-deepseek-key",
        "google_spreadsheet_id": EXPECTED_SPREADSHEET_ID,
    }


def test_valid_payload_is_accepted_without_modifying_secret_values():
    payload = valid_payload()
    assert validate_payload(payload) == payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vless_reality_url", "https://example.test"),
        ("telegram_bot_token", "not-a-token"),
        ("deepseek_api_key", "   "),
        ("google_spreadsheet_id", "wrong-sheet"),
    ],
)
def test_invalid_scalar_secret_fields_are_rejected(field, value):
    payload = valid_payload()
    payload[field] = value
    with pytest.raises(ValueError):
        validate_payload(payload)


@pytest.mark.parametrize("missing", ["type", "client_email", "private_key"])
def test_google_service_account_requires_expected_type_and_fields(missing):
    payload = valid_payload()
    del payload["google_service_account"][missing]
    with pytest.raises(ValueError):
        validate_payload(payload)


def test_google_json_must_be_service_account():
    payload = valid_payload()
    payload["google_service_account"]["type"] = "authorized_user"
    with pytest.raises(ValueError):
        validate_payload(payload)


def test_atomic_output_is_private_and_never_overwrites(tmp_path):
    output = tmp_path / "outside-project" / "setup.json"
    atomic_write_json(output, valid_payload())

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text(encoding="utf-8")) == valid_payload()

    with pytest.raises(FileExistsError):
        atomic_write_json(output, valid_payload())


def test_state_accepts_only_one_successful_submission(tmp_path):
    state = WizardState(tmp_path / "setup.json")
    state.submit(valid_payload())
    with pytest.raises(RuntimeError):
        state.submit(valid_payload())


def test_failed_submission_does_not_consume_the_single_attempt(tmp_path):
    state = WizardState(tmp_path / "setup.json")
    bad = valid_payload()
    bad["telegram_bot_token"] = "bad"
    with pytest.raises(ValueError):
        state.submit(bad)

    state.submit(valid_payload())
    assert state.completed is True


def test_wizard_is_mobile_russian_step_by_step_and_reads_file_in_browser():
    html = render_wizard("/random-secret-path")
    assert '<meta name="viewport"' in html
    assert "Настройка сервера" in html
    assert 'type="file"' in html
    assert "application/json" in html
    assert ".text()" in html
    assert EXPECTED_SPREADSHEET_ID in html
    assert "Готово" in html
    assert "localStorage" not in html
    assert "sessionStorage" not in html


def test_security_headers_disable_storage_frames_and_sniffing():
    headers = dict(security_headers())
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_cli_has_fixed_bind_and_requires_secret_path_and_output(monkeypatch):
    monkeypatch.delenv("SETUP_WIZARD_PATH", raising=False)
    monkeypatch.delenv("SETUP_WIZARD_OUTPUT", raising=False)
    parser = build_parser()
    args = parser.parse_args(["--secret-path", "/abc", "--output", "/tmp/out.json"])
    assert args.secret_path == "/abc"
    assert args.output == Path("/tmp/out.json")
    assert not hasattr(args, "host")
    assert not hasattr(args, "port")


def test_body_limit_is_exactly_128_kib():
    assert MAX_BODY_BYTES == 128 * 1024


def test_no_secret_names_are_exported_to_process_environment(tmp_path):
    before = os.environ.copy()
    WizardState(tmp_path / "setup.json").submit(valid_payload())
    assert os.environ == before
