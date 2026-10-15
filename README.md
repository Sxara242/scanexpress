# ScanExpress

ScanExpress is a web app for triggering scanner jobs (for example a Brother ADS-2200 connected through a Raspberry Pi) from a browser, then sending scanned output to a Paperless-ngx server.

## Design

- Backend + web serving: Python (Flask)
- Frontend: JavaScript + HTML (served by Flask)
- Target integration: Scanner hardware via host device (Raspberry Pi or equivalent)
- Output destination: Paperless-ngx

## Development and Testing

For local development setup, Playwright e2e smoke tests, and iterative deployment workflows, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

For current production UI requirements and acceptance criteria, see [docs/UI_FINALIZATION_SPEC.md](docs/UI_FINALIZATION_SPEC.md).

## Scanner Manager fork status

This fork is evolving ScanExpress into the physical-button and Pending-review
workflow described in `docs/SCANNER_MANAGER_MASTER_SPEC.md`.

The reference production deployment uses a systemd-managed Gunicorn process
bound to loopback on port 8010. The service template initializes persistent state under
`/var/lib/scanexpress` before startup. Set a real `secret_key` (or
`SCANEXPRESS_SECRET_KEY`) before production startup; the WSGI entry point
fails closed when it is missing.

Captured Pending documents and the SQLite state database belong in the
configured data directory, never in the Git working tree.

Phase work is developed on feature branches and merged only after CI passes.

## Server Installation

Use this section for one-time server setup. Use the development guide for iterative update workflows.

Prerequisites:

- Server has scanner drivers/tools installed and can run `scanimage`.
- Python 3 with venv support is available.
- Pillow is installed through `requirements.txt`; a system `python3-pil` package is not required for the venv.

Install required system packages:

    sudo apt update
    sudo apt install -y python3-venv

One-time app install:

    sudo mkdir -p /opt/scanexpress
    sudo chown "$USER":"$USER" /opt/scanexpress
    git clone https://github.com/Sxara242/scanexpress /opt/scanexpress
    cd /opt/scanexpress
    python3 -m venv --system-site-packages .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Note: with `--system-site-packages`, `pip` may warn about unrelated global package conflicts.

Optional (isolated venv, no global packages):

    pip install -r requirements.txt "Pillow>=10.3,<13.0"

Create service account and group:

    sudo groupadd --system scanexpress
    sudo useradd --system --gid scanexpress --create-home --home /var/lib/scanexpress --shell /usr/sbin/nologin scanexpress

For physical-button deployments, the reference service runs the process with
primary group `scanner` so the scanbd user/group can reach the local Unix
event socket, while retaining `scanexpress` as a supplementary group for
reading the protected configuration file.

Create config file from sample:

    sudo cp /opt/scanexpress/scanexpress.sample.conf /etc/scanexpress.conf
    sudo chown root:scanexpress /etc/scanexpress.conf
    sudo chmod 640 /etc/scanexpress.conf

Edit `/etc/scanexpress.conf` and set `global.default_user`, `global.paperless_base_url`, real user tokens, and scanner device templates.

## Docker Image

[Versioned Docker images](https://hub.docker.com/r/baztian/scanexpress) are published to Docker Hub from release tags (`vMAJOR.MINOR.PATCH`).

The image includes `scanimage` (`sane-utils`) so scanner commands can run inside the container.

Example:

        docker pull baztian/scanexpress:latest
        docker run --rm -p 8000:8000 \
            -v /etc/scanexpress.conf:/etc/scanexpress.conf:ro \
            baztian/scanexpress:latest

### USB scanner permissions and mounts

For local USB scanners, run with USB and udev mounts:

    docker run --rm -p 8000:8000 \
        --device /dev/bus/usb:/dev/bus/usb \
        -v /run/udev:/run/udev:ro \
        -v /etc/scanexpress.conf:/etc/scanexpress.conf:ro \
        baztian/scanexpress:latest

## Authentication and Session Secret

ScanExpress requires `global.default_user` to be set for normal UI operation.

- Set `global.default_user = <username>` to choose the active UI user.
- If `global.default_user` is empty or missing, the UI renders a configuration error page.

### Admin instructions

1. Configure users in `/etc/scanexpress.conf`:

       [global]
       # Set to a long random value before deployment.
       # secret_key = replace-with-long-random-secret
       default_user = alice
       paperless_base_url = https://paperless.example.com

       [user:alice]
       password_hash = scrypt:32768:8:1$...$...
       paperless_api_token = replace-with-alice-token
       default_device = brother-color

2. Generate password hashes (run on a trusted host):

    python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('change-me'))"

3. Configure Flask session signing secret in `/etc/scanexpress.conf` (`[global].secret_key`).

    Optional override: set `SCANEXPRESS_SECRET_KEY` in your service environment; this takes precedence over config.

4. Reload/restart service:

    sudo systemctl daemon-reload
    sudo systemctl restart scanexpress

Practical Unix shell example to generate a strong random secret:

    openssl rand -base64 48 | tr -d '\n'

Alternative without `openssl`:

    python3 -c "import secrets; print(secrets.token_urlsafe(64))"

### User instructions

- Open the ScanExpress UI in browser.
- Your username appears in the top-right header.
- Use `Log out` in the top-right header to end your session.
- For a frontend-only walkthrough, open the UI with `?demo=1` (for example `/\?demo=1`) to load sample devices and recent uploads without running a real scan.

Optional: if your config file is stored at a non-default path, set `SCANEXPRESS_CONFIG_FILE` in your service unit environment.

Install service from `scanexpress.service.template`:

    sudo cp /opt/scanexpress/scanexpress.service.template /etc/systemd/system/scanexpress.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now scanexpress
    sudo systemctl status scanexpress --no-pager

Quick validation:

    curl -sS -X POST http://127.0.0.1:8010/api/scan
    sudo journalctl -u scanexpress -n 80 --no-pager

## Backend Scan + Upload Configuration

`POST /api/scan` now performs this pipeline:

1. Execute scanner command using device-configured output mode:
    - `scan_output_mode = batch` -> `--batch=/tmp/.../scan_output%d.tiff`
    - `scan_output_mode = single_file` -> `--output-file=/tmp/.../scan_output.tiff`
2. Convert all generated TIFF pages/files to a single PDF
3. Upload PDF to Paperless-ngx `/api/documents/post_document/`

For live UI progress updates, the frontend uses `POST /api/scan/stream` (NDJSON stream) and updates status as scan pages complete.

On successful completion, scan responses include `timing_metrics` (`total_seconds`, `scan_seconds`, `paperless_seconds`, `scan_seconds_per_page`, `paperless_seconds_per_page`) and the UI status message includes these values.

`POST /api/scan` and `POST /api/scan/stream` success payloads now also include:

- `paperless_task_id` (UUID string or `null`), parsed from Paperless upload responses including raw JSON UUID-string responses.

UI recent upload tracking:

- The page shows a `Recent uploads` list (latest 10 entries, newest first).
- Recent uploads are stored in server memory per configured user and loaded via `GET /api/recent-uploads`.
- Refreshing the page keeps that user's recent uploads visible; entries are not shared across users.
- For each entry with `paperless_task_id`, the frontend polls backend task status every 2 seconds until a terminal state.
- Polling uses backend proxy endpoint `GET /api/paperless/tasks/<task_id>` so browser clients never call Paperless directly with tokens.
- On `SUCCESS` with `related_document`, the UI renders a link to `<paperless_base_url>/documents/<related_document>`.

Configuration resolution is **config-first with env fallback**:

- `config.ini` user + device sections are primary source for token, scan command, device id, per-device scan timeout, and scan output mode.
- `config.ini` `:scanimage-params` subsection is the preferred source for scanner command options; keys are passed through dynamically as CLI args.
- `config.ini` `[global]` is primary source for `paperless_base_url`, `scan_timeout_seconds`, and `paperless_timeout_seconds`.
- `config.ini` `[global]` also defines `default_user` (required active UI user).
- `config.ini` `[user:<username>]` supports:
  - `default_device` (required when one or more device templates are available to that user) to choose the default scanner template deterministically.
  - `default_scanimage_params_device` (optional) to choose which device template supplies default `scanimage` parameters.
- Device templates can be defined either per user or globally:
  - user-specific: `[user:<username>:device:<device_name>]`
  - shared defaults for all users: `[device:<device_name>]`
  - if both exist with the same `<device_name>`, the user-specific section takes precedence.

See full schema and examples in [docs/CONFIG_SPEC.md](docs/CONFIG_SPEC.md).

Environment variables:

- `SCANEXPRESS_CONFIG_FILE` (optional): path to config file.
- `SCANEXPRESS_SECRET_KEY` (optional): overrides `[global].secret_key` when set.

Default config lookup order when `SCANEXPRESS_CONFIG_FILE` is not set:

- `~/.config/scanexpress/scanexpress.conf`
- `/etc/scanexpress.conf`

Dynamic scanner args from config:

- Preferred: define arbitrary scanner options in `[user:<username>:device:<device_name>:scanimage-params]` or `[device:<device_name>:scanimage-params]`.
- Each key/value is passed to scanner as `--<key> <value>` (underscores become dashes).
- Compatibility fallback: when `:scanimage-params` is absent, extra non-reserved keys in the selected device section are also passed as scanner args.
- If no `device_id` is configured for the selected/default template, ScanExpress runs the scan command without the `-d` argument.

Required scan output mode per device:

- Each device section (`[user:<username>:device:<device_name>]` or `[device:<device_name>]`) must define `scan_output_mode` as either `batch` or `single_file`.
- `batch` and `single_file` map to mutually exclusive scanner flags (`--batch` vs `--output-file`).

## License

MIT (see [LICENSE](LICENSE)).
