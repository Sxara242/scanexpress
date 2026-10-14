import json
import os
import random
import re
import select
import shlex
import subprocess
import tempfile
import threading
import time
from functools import wraps
from queue import Queue
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file, session, stream_with_context
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user
from config import ConfigManager
from storage import (
    apply_paperless_task_status,
    create_pending_document,
    get_pending_document,
    list_pending_documents,
    resolve_pending_pdf_path,
    set_pending_submission_state,
    update_pending_metadata,
)

try:
    from PIL import Image, ImageFile, UnidentifiedImageError
    ImageFile.LOAD_TRUNCATED_IMAGES = True
except ImportError:
    Image = None
    UnidentifiedImageError = OSError

app = Flask(__name__)
_CONFIG_MANAGER: ConfigManager | None = None
_TIMEOUT_COUNTDOWN_START_SECONDS = 15
_DEVICE_SCAN_LOCK = threading.Lock()
_ACTIVE_DEVICE_SCANS: dict[str, dict[str, object]] = {}
_PAPERLESS_UPLOAD_MAX_ATTEMPTS = 3
_PAPERLESS_UPLOAD_RETRY_BASE_DELAY_SECONDS = 0.5
_PAPERLESS_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DEFAULT_FILENAME_TEMPLATE = "scan_{scan_uuid}"
_MAX_RECENT_UPLOADS = 10
_RECENT_UPLOADS_LOCK = threading.Lock()
_RECENT_UPLOADS_BY_USER: dict[str, list[dict[str, object | None]]] = {}
_DEFAULT_AUTH_REALM = "ScanExpress"
_BASIC_AUTH_SUPPRESSED_SESSION_KEY = "basic_auth_suppressed"


class ScanExpressUser(UserMixin):
    def __init__(self, username: str):
        self.id = username


_LOGIN_MANAGER = LoginManager()
_LOGIN_MANAGER.init_app(app)


class ScanInProgressError(RuntimeError):
    pass


def _current_time_millis() -> int:
    return int(time.time() * 1000)


def _build_recent_upload_defaults(task_id: str) -> dict[str, object | None]:
    now_millis = _current_time_millis()
    return {
        "task_id": task_id,
        "submitted_at": now_millis,
        "device_name": None,
        "file_name": None,
        "task_status": "STARTED",
        "result_text": None,
        "related_document": None,
        "is_polling": True,
        "last_error": None,
        "last_updated_at": now_millis,
        "poll_failure_count": 0,
    }


def _upsert_recent_upload_for_user(
    username: str,
    task_id: str,
    updates: dict[str, object | None] | None = None,
) -> dict[str, object | None]:
    normalized_task_id = task_id.strip()
    if normalized_task_id == "":
        raise RuntimeError("task_id is required for recent upload history.")

    next_updates = dict(updates or {})
    next_updates["task_id"] = normalized_task_id
    if "last_updated_at" not in next_updates:
        next_updates["last_updated_at"] = _current_time_millis()

    with _RECENT_UPLOADS_LOCK:
        user_history = _RECENT_UPLOADS_BY_USER.setdefault(username, [])
        for existing_index, existing_entry in enumerate(user_history):
            if existing_entry.get("task_id") != normalized_task_id:
                continue

            merged_entry = {**existing_entry, **next_updates}
            user_history[existing_index] = merged_entry
            return dict(merged_entry)

        new_entry = {**_build_recent_upload_defaults(normalized_task_id), **next_updates}
        user_history.insert(0, new_entry)
        del user_history[_MAX_RECENT_UPLOADS:]
        return dict(new_entry)


def _list_recent_uploads_for_user(username: str) -> list[dict[str, object | None]]:
    with _RECENT_UPLOADS_LOCK:
        user_history = _RECENT_UPLOADS_BY_USER.get(username, [])
        return [dict(entry) for entry in user_history]


def _is_paperless_upload_failure_message(message: str) -> bool:
    return "Paperless upload request failed" in message or "Paperless upload failed (" in message


def _register_recent_upload_from_scan_payload(scan_payload: dict) -> None:
    if scan_payload.get("status") != "ok":
        return

    username = scan_payload.get("username")
    task_id = scan_payload.get("paperless_task_id")
    if not isinstance(username, str) or username.strip() == "":
        return
    if not isinstance(task_id, str) or task_id.strip() == "":
        return

    filename = scan_payload.get("filename")
    file_name = str(filename).strip() if filename is not None else None
    if file_name == "":
        file_name = None

    _upsert_recent_upload_for_user(
        username,
        task_id,
        {
            "submitted_at": _current_time_millis(),
            "device_name": scan_payload.get("device_name"),
            "file_name": file_name,
            "task_status": "STARTED",
            "result_text": None,
            "related_document": None,
            "is_polling": True,
            "last_error": None,
            "poll_failure_count": 0,
        },
    )


def _register_recent_upload_failure(
    username: str,
    message: str,
    device_name: str | None = None,
    file_name: str | None = None,
) -> None:
    if not _is_paperless_upload_failure_message(message):
        return

    failure_task_id = f"upload-failure-{_current_time_millis()}-{random.randint(0, 9999)}"
    _upsert_recent_upload_for_user(
        username,
        failure_task_id,
        {
            "submitted_at": _current_time_millis(),
            "device_name": device_name,
            "file_name": file_name,
            "task_status": "FAILURE",
            "result_text": message,
            "related_document": None,
            "is_polling": False,
            "last_error": None,
            "poll_failure_count": 0,
        },
    )


def get_config_manager() -> ConfigManager:
    global _CONFIG_MANAGER
    if _CONFIG_MANAGER is None:
        _CONFIG_MANAGER = ConfigManager()
    return _CONFIG_MANAGER


def _resolve_secret_key() -> str:
    configured_env_secret = os.getenv("SCANEXPRESS_SECRET_KEY", "").strip()
    if configured_env_secret != "":
        return configured_env_secret

    try:
        config_manager = get_config_manager()
        configured_secret = config_manager.get_global("secret_key")
    except RuntimeError:
        configured_secret = None

    if isinstance(configured_secret, str) and configured_secret.strip() != "":
        return configured_secret.strip()

    return "scanexpress-dev-secret"


def _has_configured_secret_key(config_manager: ConfigManager | None = None) -> bool:
    configured_env_secret = os.getenv("SCANEXPRESS_SECRET_KEY", "").strip()
    if configured_env_secret != "":
        return True

    manager = config_manager or get_config_manager()
    try:
        configured_secret = manager.get_global("secret_key")
    except RuntimeError:
        return False

    return isinstance(configured_secret, str) and configured_secret.strip() != ""


def _validate_startup_configuration(
    config_manager: ConfigManager | None = None, *, production: bool = False
) -> None:
    manager = config_manager or get_config_manager()
    default_user = _resolve_default_user(manager)
    has_secret_key = _has_configured_secret_key(manager)

    if not has_secret_key and (production or default_user is None):
        mode = "production" if production else "login"
        raise RuntimeError(
            f"ScanExpress is not configured properly for {mode} mode: set "
            "[global].secret_key or SCANEXPRESS_SECRET_KEY."
        )


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


app.secret_key = _resolve_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_env_flag("SCANEXPRESS_SECURE_COOKIES", default=False),
)


def _resolve_auth_realm() -> str:
    configured_realm = session.get("auth_realm")
    if isinstance(configured_realm, str) and configured_realm.strip() != "":
        return configured_realm.strip()
    return _DEFAULT_AUTH_REALM


def _build_unauthorized_response(message: str = "Authentication required."):
    response = jsonify({"status": "error", "message": message})
    response.headers["WWW-Authenticate"] = f'Basic realm="{_resolve_auth_realm()}"'
    return response, 401


def _resolve_default_user(config_manager: ConfigManager) -> str | None:
    configured_default_user = config_manager.get_default_user()
    if isinstance(configured_default_user, str) and configured_default_user.strip() != "":
        return configured_default_user
    return None


@_LOGIN_MANAGER.user_loader
def _load_user(user_id: str):
    if not isinstance(user_id, str) or user_id.strip() == "":
        return None

    try:
        config_manager = get_config_manager()
        if not config_manager.user_exists(user_id):
            return None
    except RuntimeError:
        return None

    return ScanExpressUser(user_id)


def _resolve_request_username() -> str:
    config_manager = get_config_manager()
    default_user = _resolve_default_user(config_manager)
    if default_user is not None:
        return default_user

    if current_user.is_authenticated:
        return str(current_user.get_id())

    raise RuntimeError("Authentication required.")


def _try_login_from_basic_auth(config_manager: ConfigManager) -> bool:
    if session.get(_BASIC_AUTH_SUPPRESSED_SESSION_KEY) is True:
        auth = request.authorization
        if auth is not None and auth.type is not None and auth.type.lower() == "basic":
            session[_BASIC_AUTH_SUPPRESSED_SESSION_KEY] = False
        return False

    auth = request.authorization
    if auth is None or auth.type is None or auth.type.lower() != "basic":
        return False

    username = (auth.username or "").strip()
    password = auth.password or ""
    if username == "":
        return False

    if not config_manager.verify_user_password(username, password):
        return False

    login_user(ScanExpressUser(username))
    session[_BASIC_AUTH_SUPPRESSED_SESSION_KEY] = False
    return True


def _auth_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            config_manager = get_config_manager()
            default_user = _resolve_default_user(config_manager)
        except RuntimeError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

        if default_user is None and not current_user.is_authenticated:
            if not _try_login_from_basic_auth(config_manager):
                return _build_unauthorized_response()

        return view(*args, **kwargs)

    return wrapped


def _render_configuration_error_page(message: str, status_code: int = 500):
    return render_template("configuration_error.html", message=message), status_code


def _encode_base62(value: int) -> str:
    if value < 0:
        raise ValueError("base62 input must be non-negative")

    if value == 0:
        return _BASE62_ALPHABET[0]

    encoded = []
    base = len(_BASE62_ALPHABET)
    while value > 0:
        value, remainder = divmod(value, base)
        encoded.append(_BASE62_ALPHABET[remainder])

    encoded.reverse()
    return "".join(encoded)


def _generate_base62_id() -> str:
    timestamp_microseconds = int(time.time() * 1_000_000)
    base62_timestamp = _encode_base62(timestamp_microseconds)
    random_part_int = random.randint(0, len(_BASE62_ALPHABET) ** 2 - 1)
    random_base62 = _encode_base62(random_part_int)
    padded_random_base62 = random_base62.zfill(2)
    return f"{base62_timestamp}{padded_random_base62}"


def _resolve_filename_template(config_manager: ConfigManager) -> str:
    configured_template = config_manager.get_filename_template()
    if not isinstance(configured_template, str) or configured_template.strip() == "":
        return _DEFAULT_FILENAME_TEMPLATE

    has_scan_uuid_placeholder = "{scan_uuid}" in configured_template
    has_base62_id_placeholder = "{base62_id}" in configured_template
    if not has_scan_uuid_placeholder and not has_base62_id_placeholder:
        return _DEFAULT_FILENAME_TEMPLATE

    return configured_template


def _build_default_filename_base(config_manager: ConfigManager) -> str:
    template = _resolve_filename_template(config_manager)
    scan_uuid = _generate_base62_id()
    return template.format(scan_uuid=scan_uuid, base62_id=scan_uuid)


def _normalize_filename_base(
    filename_base: str | None,
    config_manager: ConfigManager,
) -> str:
    if filename_base is None:
        normalized_basename = _build_default_filename_base(config_manager)
    else:
        normalized_basename = filename_base.strip()
        normalized_basename = re.sub(r"(?:\.pdf)+$", "", normalized_basename, flags=re.IGNORECASE)

    if normalized_basename == "":
        raise RuntimeError("Filename cannot be empty")

    return normalized_basename


def _is_empty_filename_input(filename_base: str) -> bool:
    normalized_basename = filename_base.strip()
    normalized_basename = re.sub(r"(?:\.pdf)+$", "", normalized_basename, flags=re.IGNORECASE)
    return normalized_basename == ""


def _resolve_libusb_device_id(scanner_device: str) -> str:
    match = re.match(r"^(?P<prefix>(?:.*:)?libusb:)(?P<device_path>/dev/.+)$", scanner_device)
    if match is None:
        return scanner_device

    device_path = match.group("device_path")
    try:
        resolved_path = Path(device_path).resolve(strict=True)
    except OSError:
        return scanner_device

    usb_path_match = re.search(
        r"/dev/bus/usb/(?P<bus>\d{3})/(?P<devnum>\d{3})$", resolved_path.as_posix()
    )
    if usb_path_match is None:
        return scanner_device

    return (
        f"{match.group('prefix')}"
        f"{usb_path_match.group('bus')}:{usb_path_match.group('devnum')}"
    )


def _build_scan_command(
    batch_output_pattern: Path, username: str | None = None, device_name: str | None = None
) -> list[str]:
    configured_command = None
    scanner_device = None
    scanimage_params: dict[str, str] = {}
    if username is not None:
        config_manager = get_config_manager()
        configured_command = config_manager.get_user_scan_command(username, device_name)
        scanner_device = config_manager.get_device_id(username, device_name)
        scanimage_params = config_manager.get_device_scanimage_params(username, device_name)

    if configured_command and configured_command.strip() != "":
        command = shlex.split(configured_command)
    else:
        command = ["scanimage"]

    if scanner_device:
        resolved_device_id = _resolve_libusb_device_id(scanner_device)
        command.extend(["-d", resolved_device_id])

    scan_output_mode = "batch"
    if username is not None:
        scan_output_mode = config_manager.get_device_scan_output_mode(username, device_name)

    for key in sorted(scanimage_params):
        option_name = key.strip().replace("_", "-")
        if not option_name:
            continue

        option_value = scanimage_params[key].strip()
        command.append(f"--{option_name}")
        command.append(option_value)

    command.append("--format=tiff")
    if scan_output_mode == "single_file":
        single_output_path = _build_single_output_tiff_path(batch_output_pattern)
        command.append(f"--output-file={single_output_path}")
    else:
        command.append(f"--batch={batch_output_pattern}")
    return command


def _resolve_scan_output_mode(username: str | None, device_name: str | None) -> str:
    if username is None:
        return "batch"
    return get_config_manager().get_device_scan_output_mode(username, device_name)


def _build_single_output_tiff_path(batch_output_pattern: Path) -> Path:
    batch_stem = batch_output_pattern.stem.replace("%d", "")
    return batch_output_pattern.parent / f"{batch_stem}.tiff"


def _resolve_scan_device_details(
    username: str | None, device_name: str | None
) -> tuple[str | None, str | None]:
    if username is None:
        return None, None

    config_manager = get_config_manager()
    configured_device_id = config_manager.get_device_id(username, device_name)
    if configured_device_id is None:
        return None, None

    scanimage_device_name = _resolve_libusb_device_id(configured_device_id)
    return configured_device_id, scanimage_device_name


def _resolve_scan_lock_device_id(
    username: str,
    device_name: str | None,
    configured_device_id: str | None = None,
    scanimage_device_name: str | None = None,
) -> str:
    if scanimage_device_name is None and configured_device_id is None:
        configured_device_id, scanimage_device_name = _resolve_scan_device_details(
            username, device_name
        )

    if scanimage_device_name is not None:
        return scanimage_device_name

    if configured_device_id is not None:
        return configured_device_id

    fallback_device_name = device_name if device_name is not None else "__default__"
    return f"__device_name__:{fallback_device_name}"


def _acquire_device_scan_lock(device_lock_id: str, username: str, device_name: str | None) -> None:
    with _DEVICE_SCAN_LOCK:
        if device_lock_id in _ACTIVE_DEVICE_SCANS:
            raise ScanInProgressError(
                f"Scan already in progress for device_id '{device_lock_id}'."
            )

        _ACTIVE_DEVICE_SCANS[device_lock_id] = {
            "username": username,
            "device_name": device_name,
            "started_at": time.time(),
        }


def _release_device_scan_lock(device_lock_id: str) -> None:
    with _DEVICE_SCAN_LOCK:
        _ACTIVE_DEVICE_SCANS.pop(device_lock_id, None)


def _build_scan_status_payload(
    requested_device_name: str | None = None,
    username: str | None = None,
) -> dict:
    config_manager = get_config_manager()
    active_username = username or _resolve_request_username()
    device_name = _resolve_requested_device_name(
        config_manager,
        active_username,
        requested_device_name,
    )
    configured_device_id, scanimage_device_name = _resolve_scan_device_details(
        active_username,
        device_name,
    )
    device_lock_id = _resolve_scan_lock_device_id(
        active_username,
        device_name,
        configured_device_id,
        scanimage_device_name,
    )

    with _DEVICE_SCAN_LOCK:
        active_scan = _ACTIVE_DEVICE_SCANS.get(device_lock_id)

    payload = {
        "status": "ok",
        "in_progress": active_scan is not None,
        "username": active_username,
        "device_name": device_name,
        "device_id": configured_device_id,
        "scanimage_device_name": scanimage_device_name,
        "device_lock_id": device_lock_id,
    }

    if active_scan is not None:
        payload["active_scan"] = active_scan

    return payload


def _process_scan_with_device_lock(
    progress_callback=None,
    requested_device_name: str | None = None,
    filename_base: str | None = None,
    username: str | None = None,
) -> dict:
    config_manager = get_config_manager()
    active_username = username or _resolve_request_username()
    device_name = _resolve_requested_device_name(
        config_manager,
        active_username,
        requested_device_name,
    )
    configured_device_id, scanimage_device_name = _resolve_scan_device_details(
        active_username,
        device_name,
    )
    device_lock_id = _resolve_scan_lock_device_id(
        active_username,
        device_name,
        configured_device_id,
        scanimage_device_name,
    )

    _acquire_device_scan_lock(device_lock_id, active_username, device_name)
    try:
        return _process_scan(
            progress_callback=progress_callback,
            requested_device_name=device_name,
            filename_base=filename_base,
            username=active_username,
        )
    finally:
        _release_device_scan_lock(device_lock_id)


def _build_device_payload(
    config_manager: ConfigManager, username: str, device_name: str
) -> dict:
    device_settings = config_manager.get_user_device(username, device_name)
    configured_device_id = device_settings.get("device_id")
    scanimage_device_name = None
    if configured_device_id is not None:
        scanimage_device_name = _resolve_libusb_device_id(configured_device_id)
    effective_scan_timeout_seconds = _resolve_scan_timeout_seconds(username, device_name)

    return {
        "device_name": device_name,
        "device_id": configured_device_id,
        "scanimage_device_name": scanimage_device_name,
        "scan_command": device_settings.get("scan_command"),
        "scan_output_mode": device_settings.get("scan_output_mode"),
        "scan_timeout_seconds": device_settings.get("scan_timeout_seconds"),
        "effective_scan_timeout_seconds": effective_scan_timeout_seconds,
        "scanimage_params": config_manager.get_device_scanimage_params(
            username, device_name
        ),
    }


def _build_device_configurations_payload(username: str | None = None) -> dict:
    config_manager = get_config_manager()
    active_username = username or _resolve_request_username()
    device_names = config_manager.list_user_devices(active_username)
    selected_device_name = config_manager.get_active_device_name(active_username)
    raw_paperless_base_url = config_manager.get_paperless_base_url()
    paperless_base_url = ""
    if isinstance(raw_paperless_base_url, str):
        paperless_base_url = raw_paperless_base_url.strip().rstrip("/")
    devices = [
        _build_device_payload(config_manager, active_username, device_name)
        for device_name in device_names
    ]

    return {
        "status": "ok",
        "username": active_username,
        "selected_device_name": selected_device_name,
        "paperless_base_url": paperless_base_url,
        "default_filename_base": _build_default_filename_base(config_manager),
        "devices": devices,
    }


def _resolve_requested_device_name(
    config_manager: ConfigManager,
    username: str,
    requested_device_name: str | None,
) -> str | None:
    configured_device_names = config_manager.list_user_devices(username)
    if requested_device_name is None:
        return config_manager.get_active_device_name(username)

    if requested_device_name in configured_device_names:
        return requested_device_name

    raise RuntimeError(
        f"Device '{requested_device_name}' is not configured for user '{username}'."
    )


def _parse_scan_progress_line(raw_line: str) -> dict | None:
    line = raw_line.strip()
    if line == "":
        return None

    scanning_page_match = re.search(r"Scanning page\s+(\d+)", line, re.IGNORECASE)
    if scanning_page_match:
        page_number = int(scanning_page_match.group(1))
        return {
            "status": "scanning",
            "message": f"Scanning page {page_number}",
            "page_count": max(page_number - 1, 0),
        }

    scanned_page_match = re.search(r"Scanned page\s+(\d+)", line, re.IGNORECASE)
    if scanned_page_match:
        page_number = int(scanned_page_match.group(1))
        return {
            "status": "scanning",
            "message": f"Scanned page {page_number}",
            "page_count": page_number,
        }

    batch_terminated_match = re.search(
        r"Batch terminated,\s*(\d+)\s*pages scanned", line, re.IGNORECASE
    )
    if batch_terminated_match:
        page_count = int(batch_terminated_match.group(1))
        return {
            "status": "scanning",
            "message": f"Batch terminated, {page_count} pages scanned",
            "page_count": page_count,
        }

    return {
        "status": "scanning",
        "message": line,
    }


def _run_scan_command(
    batch_output_pattern: Path,
    progress_callback=None,
    username: str | None = None,
    device_name: str | None = None,
) -> list[Path]:
    command = _build_scan_command(batch_output_pattern, username, device_name)
    app.logger.info("Executing scan command: %s", shlex.join(command))
    scan_timeout_seconds_per_page = _resolve_scan_timeout_seconds(username, device_name)

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Scan command not found. Configure scan_command in config.ini or install scanimage."
        ) from exc

    if process.stderr is None:
        process.kill()
        raise RuntimeError("Failed to capture scanner progress output.")

    stderr_messages = []
    last_page_progress_at = time.monotonic()
    while True:
        ready_streams, _, _ = select.select(
            [process.stderr], [], [], 1
        )
        if not ready_streams:
            elapsed_without_page_progress = time.monotonic() - last_page_progress_at
            if elapsed_without_page_progress > scan_timeout_seconds_per_page:
                process.kill()
                process.wait()
                raise RuntimeError(
                    f"Scan command timed out after {scan_timeout_seconds_per_page} seconds without page progress."
                )
            continue

        line = process.stderr.readline()
        if line == "":
            if process.poll() is not None:
                break
            continue

        stripped_line = line.strip()
        if stripped_line != "":
            stderr_messages.append(stripped_line)

        if progress_callback is not None:
            progress_update = _parse_scan_progress_line(line)
            if progress_update is not None:
                progress_callback(progress_update)
                if "page_count" in progress_update:
                    last_page_progress_at = time.monotonic()

    return_code = process.wait()
    if return_code != 0:
        stderr_message = "\n".join(stderr_messages[-3:]).strip()
        if not stderr_message:
            stderr_message = "scanner command exited with a non-zero status"
        raise RuntimeError(f"Scanner command failed: {stderr_message}")

    scan_output_mode = _resolve_scan_output_mode(username, device_name)
    if scan_output_mode == "single_file":
        output_tiff_paths = [_build_single_output_tiff_path(batch_output_pattern)]
    else:
        batch_stem = batch_output_pattern.stem.replace("%d", "")
        output_tiff_paths = list(batch_output_pattern.parent.glob(f"{batch_stem}*.tiff"))

    def _extract_batch_index(path: Path) -> int:
        match = re.search(r"(\d+)(?=\.tiff$)", path.name)
        if match is None:
            return -1
        return int(match.group(1))

    output_tiff_paths = sorted(output_tiff_paths, key=lambda path: (_extract_batch_index(path), path.name))
    if not output_tiff_paths:
        raise RuntimeError("Scanner produced no TIFF output.")

    empty_output_paths = [
        output_tiff_path
        for output_tiff_path in output_tiff_paths
        if output_tiff_path.stat().st_size == 0
    ]
    if empty_output_paths:
        non_empty_output_paths = [
            output_tiff_path
            for output_tiff_path in output_tiff_paths
            if output_tiff_path.stat().st_size > 0
        ]
        empty_output_files = [output_tiff_path.name for output_tiff_path in empty_output_paths]
        empty_files_summary = ", ".join(empty_output_files[:5])
        if len(empty_output_files) > 5:
            empty_files_summary = f"{empty_files_summary}, ..."

        if not non_empty_output_paths:
            raise RuntimeError(
                f"Scanner produced empty TIFF output file(s): {empty_files_summary}."
            )

        app.logger.warning(
            "Ignoring empty TIFF output file(s): %s",
            empty_files_summary,
        )
        output_tiff_paths = non_empty_output_paths

    return output_tiff_paths


def _convert_tiffs_to_pdf(input_tiff_paths: list[Path], output_pdf_path: Path) -> int:
    if Image is None:
        raise RuntimeError(
            "PIL is not installed. Install Pillow via pip or install python3-pil and run with system site-packages."
        )

    tiff_sizes_kb = [
        f"{input_tiff_path.name}({input_tiff_path.stat().st_size // 1024}KB)"
        for input_tiff_path in input_tiff_paths
    ]
    app.logger.info(
        "PDF conversion starting: %d TIFF file(s): %s",
        len(input_tiff_paths),
        ", ".join(tiff_sizes_kb),
    )

    page_count = 0
    wrote_first_page = False
    try:
        for input_tiff_path in input_tiff_paths:
            with Image.open(input_tiff_path) as image:
                frame_count = getattr(image, "n_frames", 1)
                for frame_index in range(frame_count):
                    image.seek(frame_index)
                    converted_page = image.convert("RGB")
                    try:
                        if not wrote_first_page:
                            converted_page.save(
                                output_pdf_path,
                                format="PDF",
                                save_all=True,
                            )
                            wrote_first_page = True
                        else:
                            converted_page.save(
                                output_pdf_path,
                                format="PDF",
                                save_all=True,
                                append=True,
                            )
                    finally:
                        converted_page.close()
                    page_count += 1
            try:
                input_tiff_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                app.logger.warning(
                    "Failed to delete temporary TIFF after conversion: %s (%s)",
                    input_tiff_path.name,
                    exc,
                )
    except FileNotFoundError as exc:
        raise RuntimeError("A TIFF file is missing for PDF conversion.") from exc
    except UnidentifiedImageError as exc:
        raise RuntimeError("Scanner output contains an invalid TIFF image.") from exc
    except OSError as exc:
        raise RuntimeError(f"Failed to read TIFF outputs: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to convert TIFF to PDF: {exc}") from exc

    if page_count == 0:
        raise RuntimeError("TIFF output contains no pages.")

    app.logger.info(
        "PDF conversion writing: %d page(s), target=%s",
        page_count,
        output_pdf_path.name,
    )

    if not output_pdf_path.exists() or output_pdf_path.stat().st_size == 0:
        raise RuntimeError("Generated PDF is empty.")

    app.logger.info(
        "PDF conversion complete: pages=%d output_size=%dKB",
        page_count,
        output_pdf_path.stat().st_size // 1024,
    )

    return page_count


def _build_paperless_upload_url(username: str) -> str:
    base_url = get_config_manager().get_paperless_base_url().strip().rstrip("/")
    if not base_url:
        raise RuntimeError("global.paperless_base_url is not configured.")
    return f"{base_url}/api/documents/post_document/"


def _extract_paperless_task_id(paperless_response: dict | None) -> str | None:
    if not isinstance(paperless_response, dict):
        return None

    for key in ["task_id", "task", "task_uuid"]:
        value = paperless_response.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip()

    raw_response = paperless_response.get("raw_response")
    if isinstance(raw_response, str) and raw_response.strip() != "":
        return raw_response.strip()

    return None


def _build_paperless_task_url(task_id: str) -> str:
    base_url = get_config_manager().get_paperless_base_url().strip().rstrip("/")
    if not base_url:
        raise RuntimeError("global.paperless_base_url is not configured.")
    return f"{base_url}/api/tasks/?task_id={task_id}"


def _normalize_paperless_task_payload(task_id: str, task_payload: dict | None) -> dict:
    task_payload = task_payload if isinstance(task_payload, dict) else {}
    related_document = task_payload.get("related_document")
    if related_document is not None:
        related_document = str(related_document)

    result = task_payload.get("result")
    if result is not None:
        result = str(result)

    date_done = task_payload.get("date_done")
    if date_done is not None:
        date_done = str(date_done)

    task_file_name = task_payload.get("task_file_name")
    if task_file_name is not None:
        task_file_name = str(task_file_name)

    return {
        "status": "ok",
        "task_id": task_id,
        "task_status": task_payload.get("status"),
        "related_document": related_document,
        "result": result,
        "date_done": date_done,
        "task_file_name": task_file_name,
        "raw_task": task_payload,
    }


def _fetch_paperless_task_status(task_id: str, username: str | None = None) -> dict:
    if username is None:
        raise RuntimeError("username is required for Paperless task status lookup.")

    api_token = get_config_manager().get_user_token(username)
    if not api_token:
        raise RuntimeError(f"No paperless API token configured for user '{username}' in config.ini.")

    task_url = _build_paperless_task_url(task_id)
    headers = {"Authorization": f"Token {api_token}"}

    try:
        response = requests.get(
            task_url,
            headers=headers,
            timeout=15,
        )
    except requests.Timeout as exc:
        raise RuntimeError("Paperless task status request timed out.") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Paperless task status request failed: {exc}") from exc

    if response.status_code >= 400:
        response_body = response.text.strip()
        if len(response_body) > 300:
            response_body = f"{response_body[:300]}..."
        if not response_body:
            response_body = response.reason or "unknown Paperless error"
        raise RuntimeError(
            f"Paperless task status failed ({response.status_code}): {response_body}"
        )

    try:
        parsed_body = response.json()
    except ValueError as exc:
        raise RuntimeError("Paperless task status response was not valid JSON.") from exc

    tasks = []
    if isinstance(parsed_body, list):
        tasks = parsed_body
    elif isinstance(parsed_body, dict):
        maybe_results = parsed_body.get("results")
        if isinstance(maybe_results, list):
            tasks = maybe_results

    for task_payload in tasks:
        if not isinstance(task_payload, dict):
            continue
        payload_task_id = task_payload.get("task_id")
        if payload_task_id is None or str(payload_task_id) == task_id:
            return _normalize_paperless_task_payload(task_id, task_payload)

    raise LookupError("Task not found")


def _calculate_paperless_timeout_seconds(page_count: int, username: str | None = None) -> int:
    timeout_per_page_seconds = None
    if username is not None:
        timeout_per_page_seconds = get_config_manager().get_paperless_timeout_seconds()
    if timeout_per_page_seconds is None:
        timeout_per_page_seconds = 5
    internal_overhead_seconds = 10
    return internal_overhead_seconds + (
        timeout_per_page_seconds * max(page_count, 1)
    )


def _resolve_scan_timeout_seconds(username: str | None, device_name: str | None = None) -> int:
    scan_timeout_seconds_per_page = None
    if username is not None:
        scan_timeout_seconds_per_page = get_config_manager().get_device_scan_timeout_seconds(
            username, device_name
        )
    if not isinstance(scan_timeout_seconds_per_page, int) or scan_timeout_seconds_per_page <= 0:
        return 30
    return scan_timeout_seconds_per_page


def _build_timing_metrics(
    total_seconds: float,
    scan_seconds: float,
    paperless_seconds: float,
    page_count: int,
) -> dict:
    effective_page_count = max(page_count, 1)
    return {
        "total_seconds": round(total_seconds, 3),
        "scan_seconds": round(scan_seconds, 3),
        "paperless_seconds": round(paperless_seconds, 3),
        "scan_seconds_per_page": round(scan_seconds / effective_page_count, 3),
        "paperless_seconds_per_page": round(paperless_seconds / effective_page_count, 3),
    }


_PAPERLESS_METADATA_ENDPOINTS = {
    "tags": "tags",
    "correspondents": "correspondents",
    "document_types": "document_types",
    "storage_paths": "storage_paths",
}


def _paperless_headers(username: str) -> dict[str, str]:
    api_token = get_config_manager().get_user_token(username)
    if not api_token:
        raise RuntimeError(f"No paperless API token configured for user '{username}' in config.ini.")
    return {"Authorization": f"Token {api_token}"}


def _paperless_api_url(path: str) -> str:
    base_url = get_config_manager().get_paperless_base_url().rstrip("/")
    normalized_path = path.lstrip("/")
    return f"{base_url}/api/{normalized_path}"


def _paperless_error_message(response, operation: str) -> str:
    body = response.text.strip()
    if len(body) > 300:
        body = f"{body[:300]}..."
    if not body:
        body = response.reason or "unknown Paperless error"
    return f"{operation} failed ({response.status_code}): {body}"


def _fetch_paperless_metadata_collection(kind: str, username: str) -> list[dict]:
    endpoint = _PAPERLESS_METADATA_ENDPOINTS.get(kind)
    if endpoint is None:
        raise ValueError("Unsupported Paperless metadata kind.")

    next_url = _paperless_api_url(f"{endpoint}/?page_size=1000")
    headers = _paperless_headers(username)
    collected: list[dict] = []
    pages_seen = 0

    while next_url and pages_seen < 20:
        pages_seen += 1
        try:
            response = requests.get(next_url, headers=headers, timeout=15)
        except requests.RequestException as exc:
            raise RuntimeError(f"Paperless metadata request failed: {exc}") from exc
        if response.status_code >= 400:
            raise RuntimeError(_paperless_error_message(response, "Paperless metadata request"))

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Paperless metadata response was not valid JSON.") from exc

        if isinstance(payload, list):
            page_items = payload
            next_url = None
        elif isinstance(payload, dict):
            page_items = payload.get("results", [])
            next_url = payload.get("next")
        else:
            raise RuntimeError("Paperless metadata response had an unexpected shape.")

        for item in page_items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            name = item.get("name")
            if item_id is None or not isinstance(name, str):
                continue
            collected.append({"id": item_id, "name": name})

    return collected


def _create_paperless_metadata_object(kind: str, name: str, username: str) -> dict:
    if kind not in {"tags", "correspondents", "document_types"}:
        raise ValueError("Creating this Paperless metadata kind is not supported.")

    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("name is required.")

    endpoint = _PAPERLESS_METADATA_ENDPOINTS[kind]
    try:
        response = requests.post(
            _paperless_api_url(f"{endpoint}/"),
            headers=_paperless_headers(username),
            json={"name": normalized_name},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Paperless metadata create request failed: {exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(_paperless_error_message(response, "Paperless metadata create"))

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Paperless metadata create response was not valid JSON.") from exc

    if not isinstance(payload, dict) or payload.get("id") is None:
        raise RuntimeError("Paperless metadata create response had an unexpected shape.")
    return {"id": payload.get("id"), "name": payload.get("name", normalized_name)}


def _build_paperless_upload_form(metadata: dict | None) -> list[tuple[str, str]]:
    if not metadata:
        return []

    form: list[tuple[str, str]] = []
    scalar_mapping = {
        "title": "title",
        "document_date": "created",
        "correspondent_id": "correspondent",
        "document_type_id": "document_type",
        "storage_path_id": "storage_path",
    }
    for source_key, paperless_key in scalar_mapping.items():
        value = metadata.get(source_key)
        if value not in (None, ""):
            form.append((paperless_key, str(value)))

    tag_ids = metadata.get("tag_ids") or []
    if not isinstance(tag_ids, list):
        raise RuntimeError("Pending tag metadata is invalid.")
    for tag_id in tag_ids:
        form.append(("tags", str(tag_id)))

    return form


def _upload_pdf_to_paperless(
    pdf_path: Path,
    page_count: int,
    username: str | None = None,
    metadata: dict | None = None,
) -> dict:
    if username is None:
        raise RuntimeError("username is required for Paperless upload.")

    upload_url = _build_paperless_upload_url(username)
    page_based_timeout_seconds = _calculate_paperless_timeout_seconds(page_count, username)
    pdf_size_bytes = pdf_path.stat().st_size
    size_based_timeout_seconds = max((pdf_size_bytes // (1024 * 1024)) + 30, 30)
    upload_timeout_seconds = max(page_based_timeout_seconds, size_based_timeout_seconds)

    app.logger.info(
        "Paperless upload starting: file=%s size=%dKB pages=%d timeout=%ds",
        pdf_path.name,
        pdf_size_bytes // 1024,
        page_count,
        upload_timeout_seconds,
    )

    headers = _paperless_headers(username)
    form_data = _build_paperless_upload_form(metadata)

    response = None
    for attempt in range(1, _PAPERLESS_UPLOAD_MAX_ATTEMPTS + 1):
        try:
            with pdf_path.open("rb") as pdf_file:
                response = requests.post(
                    upload_url,
                    headers=headers,
                    data=form_data,
                    files={"document": (pdf_path.name, pdf_file, "application/pdf")},
                    timeout=upload_timeout_seconds,
                )
        except requests.RequestException as exc:
            app.logger.warning(
                "Paperless upload attempt %d/%d failed: %s",
                attempt,
                _PAPERLESS_UPLOAD_MAX_ATTEMPTS,
                exc,
            )
            if attempt >= _PAPERLESS_UPLOAD_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"Paperless upload request failed: {exc}"
                ) from exc

            retry_delay_seconds = _PAPERLESS_UPLOAD_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            time.sleep(retry_delay_seconds)
            continue

        if response.status_code >= 400:
            response_body = response.text.strip()
            if len(response_body) > 300:
                response_body = f"{response_body[:300]}..."
            if not response_body:
                response_body = response.reason or "unknown Paperless error"

            if (
                response.status_code in _PAPERLESS_RETRYABLE_STATUS_CODES
                and attempt < _PAPERLESS_UPLOAD_MAX_ATTEMPTS
            ):
                app.logger.warning(
                    "Paperless upload attempt %d/%d returned retryable status %d",
                    attempt,
                    _PAPERLESS_UPLOAD_MAX_ATTEMPTS,
                    response.status_code,
                )
                retry_delay_seconds = _PAPERLESS_UPLOAD_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                time.sleep(retry_delay_seconds)
                continue

            raise RuntimeError(
                f"Paperless upload failed ({response.status_code}): {response_body}"
            )

        break

    if response is None:
        raise RuntimeError("Paperless upload failed before receiving a response.")

    try:
        parsed_body = response.json()
    except ValueError:
        parsed_body = {}

    if isinstance(parsed_body, dict):
        return parsed_body
    if isinstance(parsed_body, str):
        parsed_value = parsed_body.strip()
        if parsed_value:
            return {"raw_response": parsed_value, "task_id": parsed_value}
    return {}


def _process_scan(
    progress_callback=None,
    requested_device_name: str | None = None,
    filename_base: str | None = None,
    username: str | None = None,
) -> dict:
    config_manager = get_config_manager()
    active_username = username or _resolve_request_username()
    device_name = _resolve_requested_device_name(
        config_manager,
        active_username,
        requested_device_name,
    )
    configured_device_id, scanimage_device_name = _resolve_scan_device_details(
        active_username, device_name
    )
    scan_timeout_seconds = _resolve_scan_timeout_seconds(active_username, device_name)
    normalized_filename_base = _normalize_filename_base(filename_base, config_manager)
    output_filename = f"{normalized_filename_base}.pdf"
    total_started_at = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="scanexpress-") as working_dir:
        working_dir_path = Path(working_dir)
        batch_output_pattern = working_dir_path / "scan_output%d.tiff"
        pdf_path = working_dir_path / output_filename

        if progress_callback is not None:
            progress_callback(
                {
                    "status": "scanning",
                    "message": "Starting scan...",
                    "scan_timeout_seconds": scan_timeout_seconds,
                    "timeout_countdown_start_seconds": _TIMEOUT_COUNTDOWN_START_SECONDS,
                }
            )

        scan_started_at = time.monotonic()
        tiff_paths = _run_scan_command(
            batch_output_pattern,
            progress_callback,
            active_username,
            device_name,
        )
        scan_elapsed_seconds = time.monotonic() - scan_started_at

        if progress_callback is not None:
            progress_callback(
                {
                    "status": "processing",
                    "message": f"Converting {len(tiff_paths)} TIFF file(s) to PDF...",
                }
            )

        page_count = _convert_tiffs_to_pdf(tiff_paths, pdf_path)
        pending = create_pending_document(
            pdf_path,
            username=active_username,
            original_filename=output_filename,
            page_count=page_count,
            device_name=device_name,
            title=normalized_filename_base,
        )

    total_elapsed_seconds = time.monotonic() - total_started_at
    timing_metrics = _build_timing_metrics(
        total_elapsed_seconds,
        scan_elapsed_seconds,
        0.0,
        page_count,
    )
    message = (
        f"Scan saved to Pending. pages={page_count}"
        f" pending_id={pending['id']}"
        f" total={timing_metrics['total_seconds']}s"
        f" scan={timing_metrics['scan_seconds']}s"
    )

    return {
        "status": "ok",
        "message": message,
        "filename_base": normalized_filename_base,
        "filename": output_filename,
        "document_id": None,
        "page_count": page_count,
        "username": active_username,
        "device_name": device_name,
        "device_id": configured_device_id,
        "scanimage_device_name": scanimage_device_name,
        "timing_metrics": timing_metrics,
        "paperless_task_id": None,
        "pending_document_id": pending["id"],
        "pending_state": pending["state"],
        "review_url": f"/pending#{pending['id']}",
    }


@app.post("/auth/login")
def login():
    try:
        config_manager = get_config_manager()
        default_user = _resolve_default_user(config_manager)
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500

    if default_user is not None:
        return jsonify({"status": "ok", "username": default_user}), 200

    if not _has_configured_secret_key(config_manager):
        return (
            jsonify(
                {
                    "status": "error",
                    "message": (
                        "ScanExpress is not configured properly: set [global].secret_key "
                        "or SCANEXPRESS_SECRET_KEY when global.default_user is not set."
                    ),
                }
            ),
            500,
        )

    auth = request.authorization
    if auth is None or auth.type.lower() != "basic":
        return _build_unauthorized_response("Basic authentication credentials are required.")

    username = auth.username or ""
    password = auth.password or ""
    if not config_manager.verify_user_password(username, password):
        return _build_unauthorized_response("Invalid username or password.")

    login_user(ScanExpressUser(username))
    session["auth_realm"] = _DEFAULT_AUTH_REALM
    session[_BASIC_AUTH_SUPPRESSED_SESSION_KEY] = False
    return jsonify({"status": "ok", "username": username}), 200


@app.post("/auth/logout")
def logout():
    try:
        default_user = _resolve_default_user(get_config_manager())
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500

    if default_user is None:
        logout_user()
        session["auth_realm"] = f"{_DEFAULT_AUTH_REALM}-{_generate_base62_id()}"
        session[_BASIC_AUTH_SUPPRESSED_SESSION_KEY] = True
    return jsonify({"status": "ok"}), 200


@app.get("/")
def index():
    try:
        config_manager = get_config_manager()
        default_user = _resolve_default_user(config_manager)
    except RuntimeError as exc:
        return _render_configuration_error_page(str(exc), 500)

    if default_user is None and not current_user.is_authenticated:
        if not _has_configured_secret_key(config_manager):
            return _render_configuration_error_page(
                "ScanExpress is not configured properly: set [global].secret_key "
                "or SCANEXPRESS_SECRET_KEY when global.default_user is not set.",
                500,
            )

        if not _try_login_from_basic_auth(config_manager):
            return _build_unauthorized_response()

    return render_template(
        "index.html",
        resolved_username=_resolve_request_username(),
    )


@app.get("/pending")
@_auth_required
def pending_page():
    return render_template(
        "pending.html",
        resolved_username=_resolve_request_username(),
    )


@app.get("/api/pending")
@_auth_required
def api_list_pending():
    username = _resolve_request_username()
    include_submitted = request.args.get("include_submitted") == "1"
    return jsonify(
        {
            "status": "ok",
            "pending": list_pending_documents(
                username,
                include_submitted=include_submitted,
            ),
        }
    ), 200


@app.get("/api/pending/<pending_id>")
@_auth_required
def api_get_pending(pending_id: str):
    try:
        payload = get_pending_document(pending_id, _resolve_request_username())
        return jsonify({"status": "ok", "pending": payload}), 200
    except LookupError:
        return jsonify({"status": "error", "message": "Pending document not found."}), 404


@app.get("/api/pending/<pending_id>/file")
@_auth_required
def api_get_pending_file(pending_id: str):
    try:
        pdf_path, original_filename = resolve_pending_pdf_path(
            pending_id,
            _resolve_request_username(),
        )
    except LookupError:
        return jsonify({"status": "error", "message": "Pending document not found."}), 404
    except FileNotFoundError:
        return jsonify({"status": "error", "message": "Pending PDF is missing."}), 410

    as_attachment = request.args.get("download") == "1"
    return send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=as_attachment,
        download_name=original_filename,
        conditional=True,
    )


@app.patch("/api/pending/<pending_id>/metadata")
@_auth_required
def api_update_pending_metadata(pending_id: str):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "JSON object required."}), 400
    try:
        payload = update_pending_metadata(
            pending_id,
            _resolve_request_username(),
            body,
        )
        return jsonify({"status": "ok", "pending": payload}), 200
    except LookupError:
        return jsonify({"status": "error", "message": "Pending document not found."}), 404
    except (ValueError, RuntimeError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.get("/api/paperless/metadata")
@_auth_required
def api_get_paperless_metadata():
    username = _resolve_request_username()
    try:
        payload = {
            kind: _fetch_paperless_metadata_collection(kind, username)
            for kind in _PAPERLESS_METADATA_ENDPOINTS
        }
        return jsonify({"status": "ok", **payload}), 200
    except (ValueError, RuntimeError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 502


@app.post("/api/paperless/metadata/<kind>")
@_auth_required
def api_create_paperless_metadata(kind: str):
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    if not isinstance(name, str):
        return jsonify({"status": "error", "message": "name must be a string."}), 400
    try:
        created = _create_paperless_metadata_object(
            kind,
            name,
            _resolve_request_username(),
        )
        return jsonify({"status": "ok", "item": created}), 201
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 502


@app.post("/api/pending/<pending_id>/submit")
@_auth_required
def api_submit_pending(pending_id: str):
    username = _resolve_request_username()
    try:
        pending = get_pending_document(
            pending_id,
            username,
            include_pdf_path=True,
        )
    except LookupError:
        return jsonify({"status": "error", "message": "Pending document not found."}), 404

    if pending["state"] == "submitted":
        return jsonify({"status": "error", "message": "Pending document is already submitted."}), 409
    if pending["state"] == "submitting":
        return jsonify({"status": "busy", "message": "Paperless ingestion is already in progress."}), 409

    pdf_path = Path(pending["pdf_path"])
    try:
        response = _upload_pdf_to_paperless(
            pdf_path,
            int(pending["page_count"]),
            username,
            pending.get("metadata"),
        )
        task_id = _extract_paperless_task_id(response)
        document_id = response.get("id")
        if task_id:
            updated = set_pending_submission_state(
                pending_id,
                username,
                state="submitting",
                task_id=task_id,
            )
        elif document_id is not None:
            updated = set_pending_submission_state(
                pending_id,
                username,
                state="submitted",
                document_id=str(document_id),
            )
        else:
            raise RuntimeError("Paperless upload returned no task or document identifier.")
    except RuntimeError as exc:
        set_pending_submission_state(
            pending_id,
            username,
            state="failed",
            error=str(exc),
        )
        return jsonify({"status": "error", "message": str(exc)}), 502

    scan_payload = {
        "status": "ok",
        "username": username,
        "paperless_task_id": task_id,
        "filename": pending.get("original_filename"),
        "device_name": pending.get("device_name"),
    }
    _register_recent_upload_from_scan_payload(scan_payload)

    return jsonify(
        {
            "status": "ok",
            "pending": updated,
            "paperless_task_id": task_id,
            "document_id": document_id,
        }
    ), 200


@app.post("/api/scan")
@_auth_required
def trigger_scan():
    request_json = request.get_json(silent=True) or {}
    requested_device_name = request_json.get("device_name")
    filename_base = request_json.get("filename_base")
    if requested_device_name is not None and not isinstance(requested_device_name, str):
        return (
            jsonify({"status": "error", "message": "device_name must be a string."}),
            400,
        )
    if filename_base is not None and not isinstance(filename_base, str):
        return (
            jsonify({"status": "error", "message": "filename_base must be a string."}),
            400,
        )
    if isinstance(filename_base, str) and _is_empty_filename_input(filename_base):
        return jsonify({"status": "error", "message": "Filename cannot be empty"}), 400

    try:
        username = _resolve_request_username()
        scan_payload = _process_scan_with_device_lock(
            requested_device_name=requested_device_name,
            filename_base=filename_base,
            username=username,
        )
        _register_recent_upload_from_scan_payload(scan_payload)
        return (
            jsonify(scan_payload),
            200,
        )
    except ScanInProgressError as exc:
        return jsonify({"status": "busy", "message": str(exc)}), 409
    except RuntimeError as exc:
        app.logger.error("Scan failed (/api/scan): %s", exc)
        if _is_paperless_upload_failure_message(str(exc)):
            username = _resolve_request_username()
            normalized_filename_base = None
            if isinstance(filename_base, str) and filename_base.strip() != "":
                normalized_filename_base = _normalize_filename_base(
                    filename_base,
                    get_config_manager(),
                )
            output_file_name = (
                f"{normalized_filename_base}.pdf" if normalized_filename_base is not None else None
            )
            _register_recent_upload_failure(
                username,
                str(exc),
                requested_device_name,
                output_file_name,
            )
        if "not configured for user" in str(exc) or str(exc) == "Filename cannot be empty":
            return jsonify({"status": "error", "message": str(exc)}), 400
        return jsonify({"status": "error", "message": str(exc)}), 500
    except Exception:
        app.logger.exception("Unexpected error while processing /api/scan")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Unexpected backend error while processing scan.",
                }
            ),
            500,
        )


@app.post("/api/scan/stream")
@_auth_required
def trigger_scan_stream():
    request_json = request.get_json(silent=True) or {}
    requested_device_name = request_json.get("device_name")
    filename_base = request_json.get("filename_base")
    if requested_device_name is not None and not isinstance(requested_device_name, str):
        return (
            jsonify({"status": "error", "message": "device_name must be a string."}),
            400,
        )
    if filename_base is not None and not isinstance(filename_base, str):
        return (
            jsonify({"status": "error", "message": "filename_base must be a string."}),
            400,
        )
    if isinstance(filename_base, str) and _is_empty_filename_input(filename_base):
        return jsonify({"status": "error", "message": "Filename cannot be empty"}), 400

    config_manager = get_config_manager()
    username = _resolve_request_username()
    try:
        device_name = _resolve_requested_device_name(
            config_manager,
            username,
            requested_device_name,
        )
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    configured_device_id, scanimage_device_name = _resolve_scan_device_details(
        username,
        device_name,
    )
    device_lock_id = _resolve_scan_lock_device_id(
        username,
        device_name,
        configured_device_id,
        scanimage_device_name,
    )

    with _DEVICE_SCAN_LOCK:
        if device_lock_id in _ACTIVE_DEVICE_SCANS:
            return (
                jsonify(
                    {
                        "status": "busy",
                        "message": f"Scan already in progress for device_id '{device_lock_id}'.",
                    }
                ),
                409,
            )

    @stream_with_context
    def stream_scan_updates():
        updates_queue: Queue[dict | None] = Queue()

        def send_progress(update: dict) -> None:
            updates_queue.put(update)

        def worker() -> None:
            try:
                result = _process_scan_with_device_lock(
                    send_progress,
                    requested_device_name=device_name,
                    filename_base=filename_base,
                    username=username,
                )
                _register_recent_upload_from_scan_payload(result)
                updates_queue.put({**result, "complete": True})
            except ScanInProgressError as exc:
                updates_queue.put(
                    {
                        "status": "busy",
                        "message": str(exc),
                        "complete": True,
                    }
                )
            except RuntimeError as exc:
                app.logger.error("Scan stream worker failed: %s", exc)
                output_file_name = None
                if isinstance(filename_base, str) and filename_base.strip() != "":
                    output_file_name = f"{_normalize_filename_base(filename_base, config_manager)}.pdf"
                _register_recent_upload_failure(
                    username,
                    str(exc),
                    device_name,
                    output_file_name,
                )
                updates_queue.put(
                    {
                        "status": "error",
                        "message": str(exc),
                        "complete": True,
                    }
                )
            except Exception:
                app.logger.exception("Unexpected error while processing /api/scan/stream")
                updates_queue.put(
                    {
                        "status": "error",
                        "message": "Unexpected backend error while processing scan.",
                        "complete": True,
                    }
                )
            finally:
                updates_queue.put(None)

        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()

        while True:
            update = updates_queue.get()
            if update is None:
                break
            yield f"{json.dumps(update)}\n"

    return Response(stream_scan_updates(), mimetype="application/x-ndjson")


@app.get("/api/device-configurations")
@_auth_required
def list_device_configurations():
    try:
        return jsonify(_build_device_configurations_payload(username=_resolve_request_username())), 200
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500
    except Exception:
        app.logger.exception("Unexpected error while processing /api/device-configurations")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Unexpected backend error while loading device configurations.",
                }
            ),
            500,
        )


@app.get("/api/scan/status")
@_auth_required
def get_scan_status():
    requested_device_name = request.args.get("device_name")
    if requested_device_name is not None and not isinstance(requested_device_name, str):
        return (
            jsonify({"status": "error", "message": "device_name must be a string."}),
            400,
        )

    try:
        return jsonify(
            _build_scan_status_payload(
                requested_device_name,
                username=_resolve_request_username(),
            )
        ), 200
    except RuntimeError as exc:
        if "not configured for user" in str(exc):
            return jsonify({"status": "error", "message": str(exc)}), 400
        return jsonify({"status": "error", "message": str(exc)}), 500
    except Exception:
        app.logger.exception("Unexpected error while processing /api/scan/status")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Unexpected backend error while loading scan status.",
                }
            ),
            500,
        )


@app.get("/api/recent-uploads")
@_auth_required
def get_recent_uploads():
    try:
        username = _resolve_request_username()
        return (
            jsonify(
                {
                    "status": "ok",
                    "username": username,
                    "recent_uploads": _list_recent_uploads_for_user(username),
                }
            ),
            200,
        )
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500
    except Exception:
        app.logger.exception("Unexpected error while processing /api/recent-uploads")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Unexpected backend error while loading recent uploads.",
                }
            ),
            500,
        )


@app.get("/api/paperless/tasks/<task_id>")
@_auth_required
def get_paperless_task_status(task_id: str):
    if task_id.strip() == "":
        return jsonify({"status": "error", "message": "task_id is required."}), 400

    try:
        username = _resolve_request_username()
        payload = _fetch_paperless_task_status(task_id, username)
        task_status = payload.get("task_status")
        is_terminal = task_status in {"SUCCESS", "FAILURE"}
        _upsert_recent_upload_for_user(
            username,
            task_id,
            {
                "task_status": task_status,
                "result_text": payload.get("result"),
                "related_document": payload.get("related_document"),
                "file_name": payload.get("task_file_name"),
                "is_polling": not is_terminal,
                "last_error": None,
                "poll_failure_count": 0,
            },
        )
        apply_paperless_task_status(
            username,
            task_id,
            task_status=task_status,
            document_id=(
                str(payload.get("related_document"))
                if payload.get("related_document") is not None
                else None
            ),
            result_text=payload.get("result"),
        )
        return jsonify(payload), 200
    except LookupError:
        return jsonify({"status": "error", "message": "Task not found"}), 404
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 502
    except Exception:
        app.logger.exception("Unexpected error while processing /api/paperless/tasks/<task_id>")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Unexpected backend error while loading Paperless task status.",
                }
            ),
            500,
        )


if __name__ == "__main__":
    try:
        _validate_startup_configuration()
    except RuntimeError as exc:
        app.logger.error("Startup configuration error: %s", exc)
        raise SystemExit(1) from exc

    app.run(host="127.0.0.1", port=8000, debug=False)
