#!/usr/bin/env python3
"""One-shot loopback-only wizard for securely receiving deployment secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LOOPBACK_HOST = "127.0.0.1"
PORT = 18080
MAX_BODY_BYTES = 128 * 1024
EXPECTED_SPREADSHEET_ID = "1H-Iwm_CjjpSdDPk-UQJE6uu7PzFZKY-XsVWA0X_buxc"
TELEGRAM_TOKEN_RE = re.compile(r"^[0-9]{5,20}:[A-Za-z0-9_-]{30,100}$")
SECRET_PATH_RE = re.compile(r"^/[A-Za-z0-9_-]{16,256}/?$")
EXPECTED_KEYS = {
    "vless_reality_url",
    "google_service_account",
    "telegram_bot_token",
    "deepseek_api_key",
    "google_spreadsheet_id",
}


def security_headers() -> tuple[tuple[str, str], ...]:
    """Return headers applied to every response, including errors."""
    csp = (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; "
        "connect-src 'self'; "
        "img-src data:; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    return (
        ("Cache-Control", "no-store"),
        ("Pragma", "no-cache"),
        ("Content-Security-Policy", csp),
        ("X-Frame-Options", "DENY"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
    )


def validate_secret_path(value: str) -> str:
    if not SECRET_PATH_RE.fullmatch(value):
        raise ValueError(
            "secret path must be one 16-256 character URL segment using letters, digits, _ or -"
        )
    return value.rstrip("/") or "/"


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field}")
    return value


def validate_payload(payload: Any) -> dict[str, Any]:
    """Validate the complete browser submission without exposing values in errors."""
    if not isinstance(payload, dict) or set(payload) != EXPECTED_KEYS:
        raise ValueError("invalid payload fields")

    vless_url = _nonempty_string(payload["vless_reality_url"], "VLESS URL")
    try:
        parsed_vless = urlsplit(vless_url)
    except ValueError as exc:
        raise ValueError("invalid VLESS URL") from exc
    if parsed_vless.scheme != "vless" or not parsed_vless.netloc:
        raise ValueError("invalid VLESS URL")

    token = _nonempty_string(payload["telegram_bot_token"], "Telegram token")
    if not TELEGRAM_TOKEN_RE.fullmatch(token):
        raise ValueError("invalid Telegram token")

    _nonempty_string(payload["deepseek_api_key"], "DeepSeek key")

    if payload["google_spreadsheet_id"] != EXPECTED_SPREADSHEET_ID:
        raise ValueError("invalid spreadsheet ID")

    google = payload["google_service_account"]
    if not isinstance(google, dict) or google.get("type") != "service_account":
        raise ValueError("invalid Google service account")
    _nonempty_string(google.get("client_email"), "Google client email")
    _nonempty_string(google.get("private_key"), "Google private key")
    return payload


def atomic_write_json(output: Path, payload: dict[str, Any]) -> None:
    """Atomically publish a mode-0600 JSON file, refusing to overwrite anything."""
    output = output.expanduser()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        os.link(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class WizardState:
    """Serialize validation and one-time publication across request handlers."""

    def __init__(self, output: Path):
        self.output = output
        self._completed = False
        self._processing = False
        self._lock = threading.Lock()

    @property
    def completed(self) -> bool:
        with self._lock:
            return self._completed

    def submit(self, payload: Any) -> None:
        with self._lock:
            if self._completed or self._processing:
                raise RuntimeError("submission already received")
            self._processing = True
        try:
            validated = validate_payload(payload)
            atomic_write_json(self.output, validated)
        except Exception:
            with self._lock:
                self._processing = False
            raise
        with self._lock:
            self._completed = True
            self._processing = False


def render_wizard(secret_path: str) -> str:
    endpoint = json.dumps(secret_path)
    spreadsheet = json.dumps(EXPECTED_SPREADSHEET_ID)
    return f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Настройка сервера</title>
<style>
:root{{--bg:#f4f7fb;--card:#fff;--ink:#172033;--muted:#64748b;--accent:#2563eb;--bad:#b91c1c}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,sans-serif}}
main{{min-height:100dvh;display:grid;place-items:center;padding:18px}} .card{{width:min(100%,480px);background:var(--card);border-radius:20px;padding:24px;box-shadow:0 18px 55px #1720331a}}
h1{{font-size:24px;margin:0 0 6px}} .progress,.hint{{color:var(--muted);font-size:14px}} .bar{{height:7px;background:#e2e8f0;border-radius:8px;overflow:hidden;margin:18px 0 24px}} .fill{{height:100%;width:20%;background:var(--accent);transition:width .2s}}
.step{{display:none}} .step.active{{display:block}} label{{display:block;font-weight:700;margin-bottom:10px}} input{{width:100%;border:1px solid #cbd5e1;border-radius:12px;padding:13px;font:inherit}} input:focus{{outline:3px solid #93c5fd;border-color:var(--accent)}}
.actions{{display:flex;gap:10px;margin-top:22px}} button{{border:0;border-radius:12px;padding:13px 18px;font:700 16px system-ui;cursor:pointer}} .next{{background:var(--accent);color:white;margin-left:auto}} .back{{background:#e2e8f0;color:var(--ink)}} .error{{min-height:24px;color:var(--bad);font-size:14px;margin-top:12px}} .done{{text-align:center;padding:28px 0}} .done h2{{font-size:30px}}
</style></head><body><main><section class="card">
<div id="wizard"><div class="progress" id="progress">Шаг 1 из 5</div><div class="bar"><div class="fill" id="fill"></div></div><h1>Настройка сервера</h1><p class="hint">Данные уйдут напрямую на сервер и не попадут в чат.</p>
<div class="step active"><label for="vless">VLESS Reality URL</label><input id="vless" type="url" inputmode="url" autocomplete="off" placeholder="vless://…" required></div>
<div class="step"><label for="google">JSON-файл сервисного аккаунта Google</label><input id="google" type="file" accept="application/json,.json" required><p class="hint">Файл читается только браузером и отправляется при завершении.</p></div>
<div class="step"><label for="telegram">Токен Telegram-бота</label><input id="telegram" type="password" autocomplete="off" spellcheck="false" required></div>
<div class="step"><label for="deepseek">API-ключ DeepSeek</label><input id="deepseek" type="password" autocomplete="off" spellcheck="false" required></div>
<div class="step"><label for="sheet">ID Google-таблицы</label><input id="sheet" type="text" readonly value={spreadsheet}></div>
<div class="error" id="error" role="alert"></div><div class="actions"><button class="back" id="back" type="button" hidden>Назад</button><button class="next" id="next" type="button">Далее</button></div></div>
<div class="done" id="done" hidden><h2>Готово</h2><p>Секреты безопасно переданы на сервер. Эту страницу можно закрыть.</p></div>
</section></main><script>
'use strict';
const endpoint={endpoint}, steps=[...document.querySelectorAll('.step')]; let current=0, googleJson=null;
const error=document.getElementById('error'), next=document.getElementById('next'), back=document.getElementById('back');
function paint(){{steps.forEach((s,i)=>s.classList.toggle('active',i===current));document.getElementById('progress').textContent=`Шаг ${{current+1}} из ${{steps.length}}`;document.getElementById('fill').style.width=`${{(current+1)*20}}%`;back.hidden=current===0;next.textContent=current===steps.length-1?'Передать':'Далее';error.textContent='';}}
async function check(){{const input=steps[current].querySelector('input');if(!input.checkValidity()){{input.reportValidity();return false}}if(current===0&&!input.value.startsWith('vless://')){{error.textContent='Ссылка должна начинаться с vless://';return false}}if(current===1){{try{{googleJson=JSON.parse(await input.files[0].text());if(googleJson.type!=='service_account')throw new Error()}}catch(_){{googleJson=null;error.textContent='Выберите корректный JSON сервисного аккаунта.';return false}}}}return true}}
next.addEventListener('click',async()=>{{if(!(await check()))return;if(current<steps.length-1){{current++;paint();return}}next.disabled=true;back.disabled=true;error.textContent='Передача…';const payload={{vless_reality_url:document.getElementById('vless').value,google_service_account:googleJson,telegram_bot_token:document.getElementById('telegram').value,deepseek_api_key:document.getElementById('deepseek').value,google_spreadsheet_id:document.getElementById('sheet').value}};try{{const response=await fetch(endpoint,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload),cache:'no-store',credentials:'same-origin'}});if(!response.ok)throw new Error();document.getElementById('wizard').remove();document.getElementById('done').hidden=false}}catch(_){{error.textContent='Не удалось передать данные. Проверьте поля и попробуйте ещё раз.';next.disabled=false;back.disabled=false}}}});
back.addEventListener('click',()=>{{if(current>0){{current--;paint()}}}});paint();
</script></body></html>"""


class WizardRequestHandler(BaseHTTPRequestHandler):
    server_version = "SetupWizard"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        """Disable request logging because the URL path is a secret."""

    @property
    def wizard_server(self) -> WizardHTTPServer:
        return self.server  # type: ignore[return-value]

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in security_headers():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, status: HTTPStatus, message: str) -> None:
        body = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _path_matches(self) -> bool:
        return self.path == self.wizard_server.secret_path

    def do_GET(self) -> None:
        if not self._path_matches():
            self._json_error(HTTPStatus.NOT_FOUND, "Не найдено")
            return
        if self.wizard_server.state.completed:
            body = (
                "<!doctype html><meta charset=utf-8><title>Готово</title><h1>Готово</h1>".encode()
            )
            self._send(HTTPStatus.GONE, body, "text/html; charset=utf-8")
            return
        body = render_wizard(self.wizard_server.secret_path).encode()
        self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        if not self._path_matches():
            self._json_error(HTTPStatus.NOT_FOUND, "Не найдено")
            return
        if self.wizard_server.state.completed:
            self._json_error(HTTPStatus.CONFLICT, "Данные уже приняты")
            return
        if self.headers.get_content_type() != "application/json":
            self._json_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Нужен JSON")
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self._json_error(HTTPStatus.LENGTH_REQUIRED, "Нужен Content-Length")
            return
        if length > MAX_BODY_BYTES:
            self._json_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Запрос слишком большой")
            return
        try:
            payload = json.loads(self.rfile.read(length))
            self.wizard_server.state.submit(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._json_error(HTTPStatus.BAD_REQUEST, "Проверьте заполненные данные")
            return
        except (RuntimeError, FileExistsError):
            self._json_error(HTTPStatus.CONFLICT, "Данные уже приняты")
            return
        except OSError:
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Не удалось сохранить данные")
            return

        self._send(HTTPStatus.CREATED, b'{"ok":true}', "application/json; charset=utf-8")
        if self.wizard_server.shutdown_on_success:
            threading.Thread(target=self.wizard_server.shutdown, daemon=True).start()


class WizardHTTPServer(HTTPServer):
    def __init__(
        self,
        secret_path: str,
        state: WizardState,
        *,
        shutdown_on_success: bool = True,
    ):
        self.secret_path = validate_secret_path(secret_path)
        self.state = state
        self.shutdown_on_success = shutdown_on_success
        super().__init__((LOOPBACK_HOST, PORT), WizardRequestHandler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One-shot loopback setup wizard (always 127.0.0.1:18080)"
    )
    parser.add_argument("--secret-path", default=os.environ.get("SETUP_WIZARD_PATH"))
    parser.add_argument("--output", type=Path, default=os.environ.get("SETUP_WIZARD_OUTPUT"))
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="do not stop automatically after the successful POST",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.secret_path:
        parser.error("set --secret-path or SETUP_WIZARD_PATH")
    if not args.output:
        parser.error("set --output or SETUP_WIZARD_OUTPUT")
    try:
        secret_path = validate_secret_path(args.secret_path)
    except ValueError as exc:
        parser.error(str(exc))
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        parser.error("output already exists; refusing to overwrite it")

    server = WizardHTTPServer(
        secret_path,
        WizardState(output),
        shutdown_on_success=not args.keep_running,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
