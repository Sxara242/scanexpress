import tempfile
import unittest
from base64 import b64encode
from pathlib import Path
from unittest.mock import Mock, patch

import app as scan_app
from config import ConfigManager


class BatchScanCommandTests(unittest.TestCase):
    def test_build_scan_command_adds_dynamic_scanimage_params(self):
        fake_config_manager = Mock()
        fake_config_manager.get_user_scan_command.return_value = "/usr/bin/scanimage"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"
        fake_config_manager.get_device_scan_output_mode.return_value = "batch"
        fake_config_manager.get_device_scanimage_params.return_value = {
            "resolution": "300",
            "mode": "Gray",
            "contrast_adjustment": "10",
            "source": "Black & White",
        }

        with patch("app.get_config_manager", return_value=fake_config_manager):
            command = scan_app._build_scan_command(
                Path("/tmp/scan_output%d.tiff"),
                username="alice",
                device_name="brother-bw",
            )

        self.assertIn("-d", command)
        self.assertIn("BrotherADS2200:libusb:001:002", command)
        self.assertIn("--resolution", command)
        self.assertIn("300", command)
        self.assertIn("--mode", command)
        self.assertIn("Gray", command)
        self.assertIn("--contrast-adjustment", command)
        self.assertIn("10", command)
        self.assertIn("--source", command)
        self.assertIn("Black & White", command)
        source_index = command.index("--source")
        self.assertEqual(command[source_index + 1], "Black & White")
        self.assertIn("--format=tiff", command)
        self.assertIn("--batch=/tmp/scan_output%d.tiff", command)

    def test_build_scan_command_omits_device_flag_when_device_id_missing(self):
        fake_config_manager = Mock()
        fake_config_manager.get_user_scan_command.return_value = "/usr/bin/scanimage"
        fake_config_manager.get_device_id.return_value = None
        fake_config_manager.get_device_scan_output_mode.return_value = "batch"
        fake_config_manager.get_device_scanimage_params.return_value = {"resolution": "300"}

        with patch("app.get_config_manager", return_value=fake_config_manager):
            command = scan_app._build_scan_command(
                Path("/tmp/scan_output%d.tiff"),
                username="alice",
                device_name="brother-bw",
            )

        self.assertNotIn("-d", command)
        self.assertIn("--resolution", command)
        self.assertIn("300", command)

    def test_build_scan_command_resolves_libusb_udev_symlink_to_bus_devnum(self):
        fake_config_manager = Mock()
        fake_config_manager.get_user_scan_command.return_value = "/usr/bin/scanimage"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:/dev/brother-scanner"
        fake_config_manager.get_device_scan_output_mode.return_value = "batch"
        fake_config_manager.get_device_scanimage_params.return_value = {}

        with patch("app.get_config_manager", return_value=fake_config_manager), patch(
            "app.Path.resolve", return_value=Path("/dev/bus/usb/001/007")
        ):
            command = scan_app._build_scan_command(
                Path("/tmp/scan_output%d.tiff"),
                username="alice",
                device_name="brother-bw",
            )

        self.assertIn("-d", command)
        self.assertIn("BrotherADS2200:libusb:001:007", command)
        self.assertNotIn("BrotherADS2200:libusb:/dev/brother-scanner", command)

    def test_build_scan_command_keeps_device_id_when_udev_link_resolution_fails(self):
        fake_config_manager = Mock()
        fake_config_manager.get_user_scan_command.return_value = "/usr/bin/scanimage"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:/dev/brother-scanner"
        fake_config_manager.get_device_scan_output_mode.return_value = "batch"
        fake_config_manager.get_device_scanimage_params.return_value = {}

        with patch("app.get_config_manager", return_value=fake_config_manager), patch(
            "app.Path.resolve", side_effect=FileNotFoundError
        ):
            command = scan_app._build_scan_command(
                Path("/tmp/scan_output%d.tiff"),
                username="alice",
                device_name="brother-bw",
            )

        self.assertIn("-d", command)
        self.assertIn("BrotherADS2200:libusb:/dev/brother-scanner", command)

    def test_resolve_scan_device_details_returns_configured_and_runtime_device_names(self):
        fake_config_manager = Mock()
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:/dev/brother-scanner"

        with patch("app.get_config_manager", return_value=fake_config_manager), patch(
            "app.Path.resolve", return_value=Path("/dev/bus/usb/001/007")
        ):
            configured_device_id, scanimage_device_name = scan_app._resolve_scan_device_details(
                "alice", "brother-bw"
            )

        self.assertEqual(configured_device_id, "BrotherADS2200:libusb:/dev/brother-scanner")
        self.assertEqual(scanimage_device_name, "BrotherADS2200:libusb:001:007")

    def test_build_scan_command_uses_output_file_for_single_file_mode(self):
        fake_config_manager = Mock()
        fake_config_manager.get_user_scan_command.return_value = "/usr/bin/scanimage"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"
        fake_config_manager.get_device_scan_output_mode.return_value = "single_file"
        fake_config_manager.get_device_scanimage_params.return_value = {"resolution": "300"}

        with patch("app.get_config_manager", return_value=fake_config_manager):
            command = scan_app._build_scan_command(
                Path("/tmp/scan_output%d.tiff"),
                username="alice",
                device_name="flatbed",
            )

        self.assertIn("--format=tiff", command)
        self.assertIn("--output-file=/tmp/scan_output.tiff", command)
        self.assertNotIn("--batch=/tmp/scan_output%d.tiff", command)

    @patch("app.time.monotonic")
    @patch("app.select.select")
    @patch("app.subprocess.Popen")
    def test_run_scan_command_returns_batch_files_in_numeric_order(
        self, mock_popen, mock_select, mock_monotonic
    ):
        mock_monotonic.side_effect = [0, 1, 2, 3, 4]
        process = FakeProcess(
            stderr_lines=[
                "Scanning page 1\n",
                "Scanned page 1. (scanner status = 5)\n",
                "Batch terminated, 1 pages scanned\n",
            ],
            returncode=0,
        )
        mock_popen.return_value = process
        mock_select.side_effect = lambda streams, _w, _x, _timeout: (
            (streams, [], []) if not process.is_complete else (streams, [], [])
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            for filename in ["scan_output10.tiff", "scan_output2.tiff", "scan_output1.tiff"]:
                (temp_path / filename).write_bytes(b"fake-tiff")

            result_paths = scan_app._run_scan_command(temp_path / "scan_output%d.tiff")

        self.assertEqual(
            [path.name for path in result_paths],
            ["scan_output1.tiff", "scan_output2.tiff", "scan_output10.tiff"],
        )

    @patch("app.time.monotonic")
    @patch("app.select.select")
    @patch("app.subprocess.Popen")
    def test_run_scan_command_rejects_empty_batch_output(
        self, mock_popen, mock_select, mock_monotonic
    ):
        mock_monotonic.side_effect = [0, 1, 2, 3, 4]
        process = FakeProcess(
            stderr_lines=[
                "Scanning page 1\n",
                "Scanned page 1. (scanner status = 5)\n",
                "Batch terminated, 1 pages scanned\n",
            ],
            returncode=0,
        )
        mock_popen.return_value = process
        mock_select.side_effect = lambda streams, _w, _x, _timeout: (
            (streams, [], []) if not process.is_complete else (streams, [], [])
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / "scan_output1.tiff").write_bytes(b"")

            with self.assertRaises(RuntimeError) as context:
                scan_app._run_scan_command(temp_path / "scan_output%d.tiff")

        self.assertIn("empty TIFF output file", str(context.exception))

    @patch("app.time.monotonic")
    @patch("app.select.select")
    @patch("app.subprocess.Popen")
    def test_run_scan_command_ignores_empty_batch_output_when_non_empty_exists(
        self, mock_popen, mock_select, mock_monotonic
    ):
        mock_monotonic.side_effect = [0, 1, 2, 3, 4]
        process = FakeProcess(
            stderr_lines=[
                "Scanning page 1\n",
                "Scanned page 1. (scanner status = 5)\n",
                "Batch terminated, 1 pages scanned\n",
            ],
            returncode=0,
        )
        mock_popen.return_value = process
        mock_select.side_effect = lambda streams, _w, _x, _timeout: (
            (streams, [], []) if not process.is_complete else (streams, [], [])
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / "scan_output1.tiff").write_bytes(b"valid")
            (temp_path / "scan_output2.tiff").write_bytes(b"")
            (temp_path / "scan_output3.tiff").write_bytes(b"")

            result_paths = scan_app._run_scan_command(temp_path / "scan_output%d.tiff")

        self.assertEqual([path.name for path in result_paths], ["scan_output1.tiff"])

    @patch("app.time.monotonic")
    @patch("app.select.select")
    @patch("app.subprocess.Popen")
    def test_run_scan_command_applies_per_page_progress_timeout(
        self, mock_popen, mock_select, mock_monotonic
    ):
        process = FakeProcess(stderr_lines=[], returncode=0)
        mock_popen.return_value = process
        mock_select.return_value = ([], [], [])
        mock_monotonic.side_effect = [0, 2]
        fake_config_manager = Mock()
        fake_config_manager.get_user_scan_command.return_value = "/usr/bin/scanimage"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"
        fake_config_manager.get_device_scan_output_mode.return_value = "batch"
        fake_config_manager.get_device_scanimage_params.return_value = {}
        fake_config_manager.get_device_scan_timeout_seconds.return_value = 1

        with patch("app.get_config_manager", return_value=fake_config_manager):
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                with self.assertRaises(RuntimeError) as context:
                    scan_app._run_scan_command(
                        temp_path / "scan_output%d.tiff",
                        username="alice",
                        device_name="brother-bw",
                    )

        self.assertIn("timed out", str(context.exception))
        self.assertTrue(process.was_killed, "Process should be killed when timeout is reached")

    @patch("app.time.monotonic")
    @patch("app.select.select")
    @patch("app.subprocess.Popen")
    def test_run_scan_command_returns_single_output_file_for_single_file_mode(
        self, mock_popen, mock_select, mock_monotonic
    ):
        mock_monotonic.side_effect = [0, 1, 2, 3, 4]
        process = FakeProcess(
            stderr_lines=[
                "Scanning...\n",
            ],
            returncode=0,
        )
        mock_popen.return_value = process
        mock_select.side_effect = lambda streams, _w, _x, _timeout: (
            (streams, [], []) if not process.is_complete else (streams, [], [])
        )
        fake_config_manager = Mock()
        fake_config_manager.get_user_scan_command.return_value = "/usr/bin/scanimage"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"
        fake_config_manager.get_device_scan_output_mode.return_value = "single_file"
        fake_config_manager.get_device_scanimage_params.return_value = {}
        fake_config_manager.get_device_scan_timeout_seconds.return_value = 30

        with patch("app.get_config_manager", return_value=fake_config_manager):
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                (temp_path / "scan_output.tiff").write_bytes(b"fake-tiff")

                result_paths = scan_app._run_scan_command(
                    temp_path / "scan_output%d.tiff",
                    username="alice",
                    device_name="flatbed",
                )

        self.assertEqual([path.name for path in result_paths], ["scan_output.tiff"])


class PaperlessTimeoutTests(unittest.TestCase):
    def test_calculate_paperless_timeout_uses_per_page_setting_with_overhead(self):
        fake_config_manager = Mock()
        fake_config_manager.get_paperless_timeout_seconds.return_value = 4
        with patch("app.get_config_manager", return_value=fake_config_manager):
            self.assertEqual(scan_app._calculate_paperless_timeout_seconds(3, "alice"), 22)

    @patch("app.time.sleep")
    @patch("app.requests.post")
    def test_upload_pdf_retries_after_timeout_and_then_succeeds(self, mock_post, mock_sleep):
        fake_config_manager = Mock()
        fake_config_manager.get_user_token.return_value = "secret-token"
        fake_config_manager.get_paperless_timeout_seconds.return_value = 5

        success_response = Mock()
        success_response.status_code = 200
        success_response.json.return_value = {"id": 4242}

        mock_post.side_effect = [
            scan_app.requests.exceptions.ReadTimeout("Read timed out"),
            success_response,
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "scan_output.pdf"
            pdf_path.write_bytes(b"fake-pdf")

            with patch("app.get_config_manager", return_value=fake_config_manager), patch(
                "app._build_paperless_upload_url", return_value="http://paperless/api/documents/post_document/"
            ):
                result = scan_app._upload_pdf_to_paperless(pdf_path, 1, "alice")

        self.assertEqual(result, {"id": 4242})
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("app.time.sleep")
    @patch("app.requests.post")
    def test_upload_pdf_retries_after_transient_server_error_and_then_succeeds(
        self, mock_post, mock_sleep
    ):
        fake_config_manager = Mock()
        fake_config_manager.get_user_token.return_value = "secret-token"
        fake_config_manager.get_paperless_timeout_seconds.return_value = 5

        transient_response = Mock()
        transient_response.status_code = 503
        transient_response.reason = "Service Unavailable"
        transient_response.text = "busy"

        success_response = Mock()
        success_response.status_code = 200
        success_response.json.return_value = {"id": 99}

        mock_post.side_effect = [transient_response, success_response]

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "scan_output.pdf"
            pdf_path.write_bytes(b"fake-pdf")

            with patch("app.get_config_manager", return_value=fake_config_manager), patch(
                "app._build_paperless_upload_url", return_value="http://paperless/api/documents/post_document/"
            ):
                result = scan_app._upload_pdf_to_paperless(pdf_path, 1, "alice")

        self.assertEqual(result, {"id": 99})
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("app.time.sleep")
    @patch("app.requests.post")
    def test_upload_pdf_returns_error_after_retry_exhaustion(self, mock_post, mock_sleep):
        fake_config_manager = Mock()
        fake_config_manager.get_user_token.return_value = "secret-token"
        fake_config_manager.get_paperless_timeout_seconds.return_value = 5

        mock_post.side_effect = scan_app.requests.exceptions.ReadTimeout("Read timed out")

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "scan_output.pdf"
            pdf_path.write_bytes(b"fake-pdf")

            with patch("app.get_config_manager", return_value=fake_config_manager), patch(
                "app._build_paperless_upload_url", return_value="http://paperless/api/documents/post_document/"
            ):
                with self.assertRaises(RuntimeError) as context:
                    scan_app._upload_pdf_to_paperless(pdf_path, 1, "alice")

        self.assertIn("Paperless upload request failed", str(context.exception))
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)


class ApiPayloadTests(unittest.TestCase):
    @patch("app.time.monotonic")
    @patch("app._upload_pdf_to_paperless", return_value={"id": 4242})
    @patch("app._convert_tiffs_to_pdf", return_value=3)
    @patch("app._run_scan_command", return_value=[Path("/tmp/scan_output1.tiff")])
    def test_process_scan_includes_device_identifiers_in_success_payload(
        self, _mock_run_scan, _mock_convert, _mock_upload, mock_monotonic
    ):
        mock_monotonic.side_effect = [10.0, 11.0, 14.0, 20.0, 22.5, 23.0]
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_active_device_name.return_value = "brother-bw"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            result = scan_app._process_scan()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["device_name"], "brother-bw")
        self.assertEqual(result["device_id"], "BrotherADS2200:libusb:001:002")
        self.assertEqual(result["scanimage_device_name"], "BrotherADS2200:libusb:001:002")
        self.assertEqual(
            result["timing_metrics"],
            {
                "total_seconds": 13.0,
                "scan_seconds": 3.0,
                "paperless_seconds": 2.5,
                "scan_seconds_per_page": 1.0,
                "paperless_seconds_per_page": 0.833,
            },
        )
        self.assertIn("total=13.0s", result["message"])
        self.assertIn("scan=3.0s", result["message"])
        self.assertIn("paperless=2.5s", result["message"])
        self.assertIn("scan_per_page=1.0s", result["message"])
        self.assertIn("paperless_per_page=0.833s", result["message"])

    @patch("app.time.monotonic")
    @patch("app._upload_pdf_to_paperless", return_value={"id": 4242, "task_id": "task-123"})
    @patch("app._convert_tiffs_to_pdf", return_value=1)
    @patch("app._run_scan_command", return_value=[Path("/tmp/scan_output1.tiff")])
    def test_process_scan_includes_paperless_task_id_from_object_response(
        self, _mock_run_scan, _mock_convert, _mock_upload, mock_monotonic
    ):
        mock_monotonic.side_effect = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_active_device_name.return_value = "brother-bw"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            result = scan_app._process_scan()

        self.assertEqual(result["paperless_task_id"], "task-123")

    @patch("app.time.monotonic")
    @patch(
        "app._upload_pdf_to_paperless",
        return_value={"raw_response": "cf13eea8-5c7a-40b8-aac8-bd8bdc315769"},
    )
    @patch("app._convert_tiffs_to_pdf", return_value=1)
    @patch("app._run_scan_command", return_value=[Path("/tmp/scan_output1.tiff")])
    def test_process_scan_includes_paperless_task_id_from_raw_uuid_response(
        self, _mock_run_scan, _mock_convert, _mock_upload, mock_monotonic
    ):
        mock_monotonic.side_effect = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_active_device_name.return_value = "brother-bw"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            result = scan_app._process_scan()

        self.assertEqual(result["paperless_task_id"], "cf13eea8-5c7a-40b8-aac8-bd8bdc315769")

    @patch("app.random.randint", return_value=1714)
    @patch("app.time.time", return_value=1730000000.0)
    @patch("app.time.monotonic")
    @patch("app._upload_pdf_to_paperless", return_value={"id": 4242})
    @patch("app._convert_tiffs_to_pdf", return_value=1)
    @patch("app._run_scan_command", return_value=[Path("/tmp/scan_output1.tiff")])
    def test_process_scan_generates_default_filename_when_filename_base_is_omitted(
        self,
        _mock_run_scan,
        _mock_convert,
        mock_upload,
        mock_monotonic,
        _mock_time,
        _mock_randint,
    ):
        mock_monotonic.side_effect = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_active_device_name.return_value = "brother-bw"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"
        fake_config_manager.get_filename_template.return_value = None

        with patch("app.get_config_manager", return_value=fake_config_manager):
            result = scan_app._process_scan()

        self.assertRegex(result["filename_base"], r"^scan_[0-9A-Za-z]+$")
        self.assertTrue(result["filename_base"].endswith("rE"))
        self.assertEqual(result["filename"], f"{result['filename_base']}.pdf")
        upload_pdf_path = mock_upload.call_args.args[0]
        self.assertEqual(upload_pdf_path.name, result["filename"])

    @patch("app.time.monotonic")
    @patch("app._upload_pdf_to_paperless", return_value={"id": 4242})
    @patch("app._convert_tiffs_to_pdf", return_value=1)
    @patch("app._run_scan_command", return_value=[Path("/tmp/scan_output1.tiff")])
    def test_process_scan_normalizes_filename_base_and_deduplicates_pdf_extension(
        self, _mock_run_scan, _mock_convert, mock_upload, mock_monotonic
    ):
        mock_monotonic.side_effect = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_active_device_name.return_value = "brother-bw"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"
        fake_config_manager.get_filename_template.return_value = None

        with patch("app.get_config_manager", return_value=fake_config_manager):
            result = scan_app._process_scan(filename_base="  invoice_2026.pdf  ")

        self.assertEqual(result["filename_base"], "invoice_2026")
        self.assertEqual(result["filename"], "invoice_2026.pdf")
        upload_pdf_path = mock_upload.call_args.args[0]
        self.assertEqual(upload_pdf_path.name, "invoice_2026.pdf")

    @patch("app.random.randint", return_value=2267)
    @patch("app.time.time", return_value=1730000001.0)
    @patch("app.time.monotonic")
    @patch("app._upload_pdf_to_paperless", return_value={"id": 4242})
    @patch("app._convert_tiffs_to_pdf", return_value=1)
    @patch("app._run_scan_command", return_value=[Path("/tmp/scan_output1.tiff")])
    def test_process_scan_uses_configured_filename_template(
        self,
        _mock_run_scan,
        _mock_convert,
        _mock_upload,
        mock_monotonic,
        _mock_time,
        _mock_randint,
    ):
        mock_monotonic.side_effect = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_active_device_name.return_value = "brother-bw"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"
        fake_config_manager.get_filename_template.return_value = "inbox_{base62_id}"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            result = scan_app._process_scan()

        self.assertRegex(result["filename_base"], r"^inbox_[0-9A-Za-z]+$")
        self.assertTrue(result["filename_base"].endswith("Az"))

    @patch("app.random.randint", return_value=2267)
    @patch("app.time.time", return_value=1730000001.0)
    @patch("app.time.monotonic")
    @patch("app._upload_pdf_to_paperless", return_value={"id": 4242})
    @patch("app._convert_tiffs_to_pdf", return_value=1)
    @patch("app._run_scan_command", return_value=[Path("/tmp/scan_output1.tiff")])
    def test_process_scan_uses_configured_scan_uuid_template(
        self,
        _mock_run_scan,
        _mock_convert,
        _mock_upload,
        mock_monotonic,
        _mock_time,
        _mock_randint,
    ):
        mock_monotonic.side_effect = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_active_device_name.return_value = "brother-bw"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"
        fake_config_manager.get_filename_template.return_value = "inbox_{scan_uuid}"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            result = scan_app._process_scan()

        self.assertRegex(result["filename_base"], r"^inbox_[0-9A-Za-z]+$")
        self.assertTrue(result["filename_base"].endswith("Az"))

    @patch("app.random.randint", return_value=1714)
    @patch("app.time.time", return_value=1730000000.0)
    @patch("app.time.monotonic")
    @patch("app._upload_pdf_to_paperless", return_value={"id": 4242})
    @patch("app._convert_tiffs_to_pdf", return_value=1)
    @patch("app._run_scan_command", return_value=[Path("/tmp/scan_output1.tiff")])
    def test_process_scan_falls_back_to_default_template_when_configured_template_invalid(
        self,
        _mock_run_scan,
        _mock_convert,
        _mock_upload,
        mock_monotonic,
        _mock_time,
        _mock_randint,
    ):
        mock_monotonic.side_effect = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_active_device_name.return_value = "brother-bw"
        fake_config_manager.get_device_id.return_value = "BrotherADS2200:libusb:001:002"
        fake_config_manager.get_filename_template.return_value = "inbox"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            result = scan_app._process_scan()

        self.assertRegex(result["filename_base"], r"^scan_[0-9A-Za-z]+$")
        self.assertTrue(result["filename_base"].endswith("rE"))


class ApiPaperlessTaskTests(unittest.TestCase):
    def setUp(self):
        scan_app.app.config["TESTING"] = True
        self.client = scan_app.app.test_client()

    def tearDown(self):
        with scan_app._RECENT_UPLOADS_LOCK:
            scan_app._RECENT_UPLOADS_BY_USER.clear()

    @patch("app.requests.get")
    def test_get_paperless_task_status_returns_normalized_started_payload(self, mock_requests_get):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_paperless_base_url.return_value = "http://paperless"
        fake_config_manager.get_user_token.return_value = "secret-token"

        task_id = "11e48898-bde8-4695-afd6-5a61452a4b54"
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "task_id": task_id,
                "status": "STARTED",
                "related_document": None,
                "result": None,
                "date_done": None,
                "task_file_name": "Offer.pdf",
            }
        ]
        mock_requests_get.return_value = mock_response

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.get(f"/api/paperless/tasks/{task_id}")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(payload["task_status"], "STARTED")
        self.assertEqual(payload["task_file_name"], "Offer.pdf")
        self.assertIsNone(payload["related_document"])

    @patch("app.requests.get")
    def test_get_paperless_task_status_returns_not_found_when_task_list_empty(self, mock_requests_get):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_paperless_base_url.return_value = "http://paperless"
        fake_config_manager.get_user_token.return_value = "secret-token"

        task_id = "cf13eea8-5c7a-40b8-aac8-bd8bdc315769"
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_requests_get.return_value = mock_response

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.get(f"/api/paperless/tasks/{task_id}")

        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["message"], "Task not found")

    @patch("app.requests.get")
    def test_get_paperless_task_status_updates_recent_upload_entry(self, mock_requests_get):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_paperless_base_url.return_value = "http://paperless"
        fake_config_manager.get_user_token.return_value = "secret-token"

        task_id = "task-123"
        scan_app._upsert_recent_upload_for_user(
            "alice",
            task_id,
            {
                "submitted_at": 1700000000000,
                "device_name": "flatbed",
                "file_name": "Offer.pdf",
                "task_status": "STARTED",
                "result_text": None,
                "related_document": None,
                "last_error": None,
            },
        )

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "task_id": task_id,
                "status": "SUCCESS",
                "related_document": 21,
                "result": "Success. New document id 21 created",
                "date_done": "2026-02-24T14:33:09.254628+01:00",
                "task_file_name": "Offer.pdf",
            }
        ]
        mock_requests_get.return_value = mock_response

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.get(f"/api/paperless/tasks/{task_id}")

        self.assertEqual(response.status_code, 200)

        with patch("app.get_config_manager", return_value=fake_config_manager):
            history_response = self.client.get("/api/recent-uploads")

        self.assertEqual(history_response.status_code, 200)
        history_payload = history_response.get_json()
        self.assertEqual(history_payload["status"], "ok")
        self.assertEqual(len(history_payload["recent_uploads"]), 1)
        history_entry = history_payload["recent_uploads"][0]
        self.assertEqual(history_entry["task_id"], task_id)
        self.assertEqual(history_entry["task_status"], "SUCCESS")
        self.assertEqual(history_entry["related_document"], "21")


class ApiRecentUploadsTests(unittest.TestCase):
    def setUp(self):
        scan_app.app.config["TESTING"] = True
        self.client = scan_app.app.test_client()

    def tearDown(self):
        with scan_app._RECENT_UPLOADS_LOCK:
            scan_app._RECENT_UPLOADS_BY_USER.clear()

    def test_get_recent_uploads_returns_empty_list_by_default(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.get("/api/recent-uploads")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["username"], "alice")
        self.assertEqual(payload["recent_uploads"], [])

    def test_get_recent_uploads_returns_entries_for_current_user_only(self):
        scan_app._upsert_recent_upload_for_user(
            "alice",
            "task-alice",
            {
                "submitted_at": 1700000000000,
                "device_name": "flatbed",
                "file_name": "alice.pdf",
                "task_status": "STARTED",
                "result_text": None,
                "related_document": None,
                "last_error": None,
            },
        )
        scan_app._upsert_recent_upload_for_user(
            "bob",
            "task-bob",
            {
                "submitted_at": 1700000001000,
                "device_name": "adf",
                "file_name": "bob.pdf",
                "task_status": "STARTED",
                "result_text": None,
                "related_document": None,
                "last_error": None,
            },
        )

        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.get("/api/recent-uploads")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["recent_uploads"]), 1)
        self.assertEqual(payload["recent_uploads"][0]["task_id"], "task-alice")


class ApiDeviceConfigurationTests(unittest.TestCase):
    def setUp(self):
        scan_app.app.config["TESTING"] = True
        self.client = scan_app.app.test_client()

    def test_get_device_configurations_returns_available_and_selected_device_details(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.list_user_devices.return_value = ["brother-bw", "brother-color"]
        fake_config_manager.get_active_device_name.return_value = "brother-bw"
        fake_config_manager.get_user_device.side_effect = [
            {
                "device_id": "scanner-bw",
                "scan_command": "/opt/scanexpress/scripts/scan_wrapper.sh",
                "scan_timeout_seconds": "30",
            },
            {
                "device_id": "scanner-color",
                "scan_command": "/opt/scanexpress/scripts/scan_wrapper.sh",
                "scan_timeout_seconds": "60",
            },
        ]
        fake_config_manager.get_device_scanimage_params.side_effect = [
            {"mode": "Gray", "resolution": "300"},
            {"mode": "Color", "resolution": "200"},
        ]

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.get("/api/device-configurations")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["username"], "alice")
        self.assertEqual(payload["selected_device_name"], "brother-bw")
        self.assertEqual(len(payload["devices"]), 2)
        self.assertEqual(payload["devices"][0]["device_name"], "brother-bw")
        self.assertEqual(payload["devices"][0]["device_id"], "scanner-bw")
        self.assertEqual(payload["devices"][0]["scanimage_params"]["mode"], "Gray")

    def test_get_device_configurations_includes_shared_device_and_prefers_user_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text(
                "[global]\n"
                "default_user = alice\n"
                "paperless_base_url = https://paperless.example.com\n"
                "\n"
                "[user:alice]\n"
                "paperless_api_token = token-alice\n"
                "default_device = shared-flatbed\n"
                "\n"
                "[device:shared-flatbed]\n"
                "device_id = global-scanner\n"
                "scan_output_mode = single_file\n"
                "scan_timeout_seconds = 40\n"
                "\n"
                "[device:shared-flatbed:scanimage-params]\n"
                "mode = Gray\n"
                "resolution = 150\n"
                "\n"
                "[user:alice:device:shared-flatbed]\n"
                "device_id = alice-scanner\n"
                "scan_output_mode = batch\n"
                "scan_timeout_seconds = 25\n"
                "\n"
                "[user:alice:device:shared-flatbed:scanimage-params]\n"
                "mode = Color\n"
                "resolution = 300\n",
                encoding="utf-8",
            )
            manager = ConfigManager(config_path)

            with patch("app.get_config_manager", return_value=manager), patch(
                "app._resolve_libusb_device_id", side_effect=lambda value: value
            ):
                response = self.client.get("/api/device-configurations")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["username"], "alice")
        self.assertEqual(payload["selected_device_name"], "shared-flatbed")
        self.assertEqual(len(payload["devices"]), 1)
        self.assertEqual(payload["devices"][0]["device_name"], "shared-flatbed")
        self.assertEqual(payload["devices"][0]["device_id"], "alice-scanner")
        self.assertEqual(payload["devices"][0]["scan_output_mode"], "batch")
        self.assertEqual(
            payload["devices"][0]["scanimage_params"],
            {"mode": "Color", "resolution": "300"},
        )

    @patch("app._upload_pdf_to_paperless", return_value={"id": 4242})
    @patch("app._convert_tiffs_to_pdf", return_value=1)
    @patch("app._run_scan_command", return_value=[Path("/tmp/scan_output1.tiff")])
    def test_post_scan_uses_requested_device_name(self, _mock_run_scan, _mock_convert, _mock_upload):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.list_user_devices.return_value = ["brother-bw", "brother-color"]
        fake_config_manager.get_device_id.return_value = "scanner-color"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.post("/api/scan", json={"device_name": "brother-color"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["device_name"], "brother-color")

    def test_post_scan_rejects_unknown_requested_device_name(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.list_user_devices.return_value = ["brother-bw"]

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.post("/api/scan", json={"device_name": "missing-device"})

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("not configured", payload["message"])

    def test_post_scan_rejects_non_string_filename_base(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.post("/api/scan", json={"filename_base": 123})

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["message"], "filename_base must be a string.")

    def test_post_scan_rejects_empty_filename_base(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.post("/api/scan", json={"filename_base": "   "})

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["message"], "Filename cannot be empty")

    @patch("app._upload_pdf_to_paperless", return_value={"id": 4242})
    @patch("app._convert_tiffs_to_pdf", return_value=1)
    @patch("app._run_scan_command", return_value=[Path("/tmp/scan_output1.tiff")])
    def test_post_scan_returns_filename_metadata(self, _mock_run_scan, _mock_convert, _mock_upload):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.list_user_devices.return_value = ["brother-bw"]
        fake_config_manager.get_device_id.return_value = "scanner-bw"
        fake_config_manager.get_filename_template.return_value = None

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.post(
                "/api/scan",
                json={"device_name": "brother-bw", "filename_base": "receipt_01"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["filename_base"], "receipt_01")
        self.assertEqual(payload["filename"], "receipt_01.pdf")


class ApiAuthTests(unittest.TestCase):
    def setUp(self):
        scan_app.app.config["TESTING"] = True
        self.client = scan_app.app.test_client()

    def _basic_auth_header(self, username: str, password: str) -> dict[str, str]:
        encoded = b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    def test_protected_api_requires_login_when_default_user_not_configured(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.get("/api/device-configurations")

        self.assertEqual(response.status_code, 401)
        payload = response.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("authentication", payload["message"].lower())

    def test_protected_api_accepts_basic_auth_and_sets_session(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.verify_user_password.return_value = True
        fake_config_manager.user_exists.return_value = True
        fake_config_manager.list_user_devices.return_value = []
        fake_config_manager.get_active_device_name.return_value = None
        fake_config_manager.get_paperless_base_url.return_value = "http://paperless"
        fake_config_manager.get_filename_template.return_value = None

        with patch("app.get_config_manager", return_value=fake_config_manager):
            first_response = self.client.get(
                "/api/device-configurations",
                headers=self._basic_auth_header("alice", "secret"),
            )
            second_response = self.client.get("/api/device-configurations")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        payload = second_response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["username"], "alice")
        fake_config_manager.verify_user_password.assert_called_once_with("alice", "secret")

    def test_index_shows_configuration_error_when_default_user_and_secret_missing(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = None

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("WWW-Authenticate", response.headers)
        body = response.get_data(as_text=True).lower()
        self.assertIn("not configured properly", body)
        self.assertIn("secret_key", body)

    def test_index_requires_browser_auth_when_default_user_missing_but_secret_configured(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = "config-secret"

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response.headers)

    def test_index_accepts_basic_auth_and_sets_session(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = "config-secret"
        fake_config_manager.verify_user_password.return_value = True
        fake_config_manager.user_exists.return_value = True

        with patch("app.get_config_manager", return_value=fake_config_manager):
            first_response = self.client.get(
                "/",
                headers=self._basic_auth_header("alice", "secret"),
            )
            second_response = self.client.get("/")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        first_body = first_response.get_data(as_text=True)
        second_body = second_response.get_data(as_text=True)
        self.assertIn("ScanExpress", first_body)
        self.assertIn("ScanExpress", second_body)
        fake_config_manager.verify_user_password.assert_called_once_with("alice", "secret")

    def test_logout_rotates_auth_realm_in_login_mode(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = "config-secret"
        fake_config_manager.verify_user_password.return_value = True
        fake_config_manager.user_exists.return_value = True

        with patch("app.get_config_manager", return_value=fake_config_manager):
            self.client.post("/auth/login", headers=self._basic_auth_header("alice", "secret"))
            logout_response = self.client.post("/auth/logout")
            index_response = self.client.get("/")

        self.assertEqual(logout_response.status_code, 200)
        self.assertEqual(index_response.status_code, 401)
        self.assertIn("WWW-Authenticate", index_response.headers)
        self.assertNotEqual(
            index_response.headers.get("WWW-Authenticate"),
            'Basic realm="ScanExpress"',
        )

    def test_logout_blocks_basic_auto_relogin_on_index(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = "config-secret"
        fake_config_manager.verify_user_password.return_value = True
        fake_config_manager.user_exists.return_value = True

        with patch("app.get_config_manager", return_value=fake_config_manager):
            self.client.get("/", headers=self._basic_auth_header("alice", "secret"))
            self.client.post("/auth/logout")
            response = self.client.get("/", headers=self._basic_auth_header("alice", "secret"))

        self.assertEqual(response.status_code, 401)

    def test_logout_allows_login_again_via_index_basic_auth(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = "config-secret"
        fake_config_manager.verify_user_password.return_value = True
        fake_config_manager.user_exists.return_value = True

        with patch("app.get_config_manager", return_value=fake_config_manager):
            self.client.get("/", headers=self._basic_auth_header("alice", "secret"))
            self.client.post("/auth/logout")
            first_retry = self.client.get("/", headers=self._basic_auth_header("alice", "secret"))
            second_retry = self.client.get("/", headers=self._basic_auth_header("alice", "secret"))

        self.assertEqual(first_retry.status_code, 401)
        self.assertEqual(second_retry.status_code, 200)
        body = second_retry.get_data(as_text=True)
        self.assertIn("ScanExpress", body)

    def test_auth_login_endpoint_clears_logout_suppression(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = "config-secret"
        fake_config_manager.verify_user_password.return_value = True
        fake_config_manager.user_exists.return_value = True

        with patch("app.get_config_manager", return_value=fake_config_manager):
            self.client.get("/", headers=self._basic_auth_header("alice", "secret"))
            self.client.post("/auth/logout")
            login_response = self.client.post(
                "/auth/login", headers=self._basic_auth_header("alice", "secret")
            )
            index_response = self.client.get("/")

        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(index_response.status_code, 200)

    def test_login_with_basic_auth_sets_session_for_protected_api(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = "config-secret"
        fake_config_manager.verify_user_password.return_value = True
        fake_config_manager.list_user_devices.return_value = []
        fake_config_manager.get_active_device_name.return_value = None
        fake_config_manager.get_paperless_base_url.return_value = "http://paperless"
        fake_config_manager.get_filename_template.return_value = None

        with patch("app.get_config_manager", return_value=fake_config_manager):
            login_response = self.client.post(
                "/auth/login", headers=self._basic_auth_header("alice", "secret")
            )
            device_response = self.client.get("/api/device-configurations")

        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(device_response.status_code, 200)
        payload = device_response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["username"], "alice")
        fake_config_manager.verify_user_password.assert_called_once_with("alice", "secret")

    def test_logout_clears_session_for_protected_api(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = "config-secret"
        fake_config_manager.verify_user_password.return_value = True
        fake_config_manager.list_user_devices.return_value = []
        fake_config_manager.get_active_device_name.return_value = None
        fake_config_manager.get_paperless_base_url.return_value = "http://paperless"
        fake_config_manager.get_filename_template.return_value = None

        with patch("app.get_config_manager", return_value=fake_config_manager):
            self.client.post("/auth/login", headers=self._basic_auth_header("alice", "secret"))
            logout_response = self.client.post("/auth/logout")
            device_response = self.client.get("/api/device-configurations")

        self.assertEqual(logout_response.status_code, 200)
        self.assertEqual(device_response.status_code, 401)

    def test_login_returns_error_when_secret_key_missing_in_login_mode(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = None

        with patch.dict("os.environ", {}, clear=True), patch(
            "app.get_config_manager", return_value=fake_config_manager
        ):
            response = self.client.post(
                "/auth/login", headers=self._basic_auth_header("alice", "secret")
            )

        self.assertEqual(response.status_code, 500)
        payload = response.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("secret_key", payload["message"])


class SecretKeyResolutionTests(unittest.TestCase):
    def test_resolve_secret_key_prefers_environment_variable(self):
        fake_config_manager = Mock()
        fake_config_manager.get_global.return_value = "config-secret"

        with patch.dict("os.environ", {"SCANEXPRESS_SECRET_KEY": "env-secret"}, clear=False), patch(
            "app.get_config_manager", return_value=fake_config_manager
        ):
            secret_key = scan_app._resolve_secret_key()

        self.assertEqual(secret_key, "env-secret")

    def test_resolve_secret_key_uses_config_when_env_missing(self):
        fake_config_manager = Mock()
        fake_config_manager.get_global.return_value = "config-secret"

        with patch.dict("os.environ", {}, clear=True), patch(
            "app.get_config_manager", return_value=fake_config_manager
        ):
            secret_key = scan_app._resolve_secret_key()

        self.assertEqual(secret_key, "config-secret")

    def test_resolve_secret_key_uses_dev_fallback_when_missing(self):
        fake_config_manager = Mock()
        fake_config_manager.get_global.return_value = None

        with patch.dict("os.environ", {}, clear=True), patch(
            "app.get_config_manager", return_value=fake_config_manager
        ):
            secret_key = scan_app._resolve_secret_key()

        self.assertEqual(secret_key, "scanexpress-dev-secret")


class StartupValidationTests(unittest.TestCase):
    def test_validate_startup_configuration_allows_login_mode_with_secret_key(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = "config-secret"

        scan_app._validate_startup_configuration(fake_config_manager)

    def test_validate_startup_configuration_raises_when_default_user_and_secret_missing(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = None
        fake_config_manager.get_global.return_value = None

        with self.assertRaises(RuntimeError) as context:
            scan_app._validate_startup_configuration(fake_config_manager)

        self.assertIn("secret_key", str(context.exception))

    def test_validate_startup_configuration_accepts_configured_default_user(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"

        scan_app._validate_startup_configuration(fake_config_manager)

    def test_validate_startup_configuration_requires_secret_in_production(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.get_global.return_value = None

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError) as context:
                scan_app._validate_startup_configuration(
                    fake_config_manager, production=True
                )

        self.assertIn("production", str(context.exception))
        self.assertIn("secret_key", str(context.exception))


class ApiScanLockTests(unittest.TestCase):
    def setUp(self):
        scan_app.app.config["TESTING"] = True
        self.client = scan_app.app.test_client()

    def tearDown(self):
        with scan_app._DEVICE_SCAN_LOCK:
            scan_app._ACTIVE_DEVICE_SCANS.clear()

    @patch("app._process_scan")
    def test_post_scan_rejects_when_same_device_is_already_scanning(self, mock_process_scan):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.list_user_devices.return_value = ["brother-bw"]
        fake_config_manager.get_device_id.return_value = "scanner-bw"

        with scan_app._DEVICE_SCAN_LOCK:
            scan_app._ACTIVE_DEVICE_SCANS["scanner-bw"] = {
                "username": "alice",
                "device_name": "brother-bw",
                "started_at": 12345.0,
            }

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.post("/api/scan", json={"device_name": "brother-bw"})

        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload["status"], "busy")
        self.assertIn("already in progress", payload["message"])
        mock_process_scan.assert_not_called()

    def test_get_scan_status_reports_device_in_progress(self):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.list_user_devices.return_value = ["brother-bw"]
        fake_config_manager.get_device_id.return_value = "scanner-bw"

        with scan_app._DEVICE_SCAN_LOCK:
            scan_app._ACTIVE_DEVICE_SCANS["scanner-bw"] = {
                "username": "alice",
                "device_name": "brother-bw",
                "started_at": 12345.0,
            }

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.get("/api/scan/status", query_string={"device_name": "brother-bw"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["in_progress"])
        self.assertEqual(payload["device_lock_id"], "scanner-bw")
        self.assertIn("active_scan", payload)

    @patch("app._process_scan")
    def test_post_scan_stream_rejects_when_same_device_is_already_scanning(self, mock_process_scan):
        fake_config_manager = Mock()
        fake_config_manager.get_default_user.return_value = "alice"
        fake_config_manager.list_user_devices.return_value = ["brother-bw"]
        fake_config_manager.get_device_id.return_value = "scanner-bw"

        with scan_app._DEVICE_SCAN_LOCK:
            scan_app._ACTIVE_DEVICE_SCANS["scanner-bw"] = {
                "username": "alice",
                "device_name": "brother-bw",
                "started_at": 12345.0,
            }

        with patch("app.get_config_manager", return_value=fake_config_manager):
            response = self.client.post("/api/scan/stream", json={"device_name": "brother-bw"})

        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload["status"], "busy")
        self.assertIn("already in progress", payload["message"])
        mock_process_scan.assert_not_called()


@unittest.skipIf(scan_app.Image is None, "Pillow is required for conversion tests")
class BatchTiffConversionTests(unittest.TestCase):
    def test_convert_tiffs_to_pdf_merges_files_and_counts_pages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            tiff_paths = [
                temp_path / "scan_output1.tiff",
                temp_path / "scan_output2.tiff",
                temp_path / "scan_output3.tiff",
            ]
            colors = ["white", "lightgray", "silver"]

            for path, color in zip(tiff_paths, colors):
                image = scan_app.Image.new("RGB", (10, 10), color)
                image.save(path, format="TIFF")

            output_pdf_path = temp_path / "scan_output.pdf"
            page_count = scan_app._convert_tiffs_to_pdf(tiff_paths, output_pdf_path)

            self.assertEqual(page_count, 3)
            self.assertTrue(output_pdf_path.exists())
            self.assertGreater(output_pdf_path.stat().st_size, 0)
            self.assertTrue(all(not tiff_path.exists() for tiff_path in tiff_paths))

    def test_convert_tiffs_to_pdf_appends_pages_incrementally(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            tiff_paths = [temp_path / "scan_output1.tiff", temp_path / "scan_output2.tiff"]
            for path in tiff_paths:
                path.write_bytes(b"fake-tiff")

            output_pdf_path = temp_path / "scan_output.pdf"
            save_calls = []
            close_calls = 0

            class FakeConvertedPage:
                def save(self, destination_path, format=None, save_all=None, **kwargs):
                    save_calls.append(
                        {
                            "destination": Path(destination_path),
                            "format": format,
                            "save_all": save_all,
                            "kwargs": kwargs,
                        }
                    )
                    mode = "ab" if kwargs.get("append") else "wb"
                    with open(destination_path, mode) as pdf_file:
                        pdf_file.write(b"%PDF-page")

                def close(self):
                    nonlocal close_calls
                    close_calls += 1

            class FakeOpenedTiff:
                n_frames = 2

                def __init__(self, _path):
                    self.frame_index = 0

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def seek(self, frame_index):
                    self.frame_index = frame_index

                def convert(self, _mode):
                    return FakeConvertedPage()

            class FakeImageModule:
                @staticmethod
                def open(path):
                    return FakeOpenedTiff(path)

            with patch("app.Image", FakeImageModule):
                page_count = scan_app._convert_tiffs_to_pdf(tiff_paths, output_pdf_path)

            self.assertEqual(page_count, 4)
            self.assertEqual(len(save_calls), 4)
            self.assertNotIn("append", save_calls[0]["kwargs"])
            self.assertNotIn("append_images", save_calls[0]["kwargs"])
            for save_call in save_calls[1:]:
                self.assertEqual(save_call["kwargs"].get("append"), True)
                self.assertNotIn("append_images", save_call["kwargs"])
            self.assertEqual(close_calls, 4)


class FakeProcess:
    def __init__(self, stderr_lines: list[str], returncode: int):
        self._stderr_lines = iter(stderr_lines)
        self.stderr = self
        self.returncode = returncode
        self.is_complete = False
        self.was_killed = False

    def readline(self) -> str:
        try:
            return next(self._stderr_lines)
        except StopIteration:
            self.is_complete = True
            return ""

    def poll(self):
        if self.is_complete:
            return self.returncode
        return None

    def wait(self):
        self.is_complete = True
        return self.returncode

    def kill(self):
        self.was_killed = True
        self.is_complete = True


if __name__ == "__main__":
    unittest.main()
