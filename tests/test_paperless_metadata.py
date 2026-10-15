"""Focused unit tests for Paperless metadata creation."""

import unittest
from unittest.mock import patch

import app as scan_app


class PaperlessMetadataCreationTests(unittest.TestCase):
    def _mock_paperless(self):
        response = unittest.mock.Mock()
        response.status_code = 201
        response.json.return_value = {
            "id": 123, "name": "Agricultural/Receipts",
            "path": "Agricultural/Receipts/{{ created_year }}/{{ title }}",
        }
        return response

    def test_create_storage_path_sends_name_and_folder_template(self):
        with patch("app._paperless_api_url", return_value="http://paperless/api/storage_paths/"), \
             patch("app._paperless_headers", return_value={"Authorization": "Token test"}), \
             patch("app.requests.post") as post:
            post.return_value = self._mock_paperless()
            created = scan_app._create_paperless_metadata_object(
                "storage_paths", " Agricultural/Receipts ", "owner",
                path=" Agricultural/Receipts/{{ created_year }}/{{ title }} ",
            )

        self.assertEqual(created["id"], 123)
        post.assert_called_once_with(
            "http://paperless/api/storage_paths/",
            headers={"Authorization": "Token test"},
            json={
                "name": "Agricultural/Receipts",
                "path": "Agricultural/Receipts/{{ created_year }}/{{ title }}",
            },
            timeout=15,
        )

    def test_storage_path_requires_nonempty_relative_template(self):
        invalid = (None, "", "  ", "/absolute", "../escape", "a/../b",
                   "a//b", r"C:\\unsafe")
        with patch("app.requests.post") as post:
            for path in invalid:
                with self.subTest(path=path):
                    with self.assertRaises(ValueError):
                        scan_app._create_paperless_metadata_object(
                            "storage_paths", "My folder", "owner", path=path,
                        )
        post.assert_not_called()

    def test_create_tag_keeps_name_only_payload(self):
        with patch("app._paperless_api_url", return_value="http://paperless/api/tags/"), \
             patch("app._paperless_headers", return_value={}), \
             patch("app.requests.post") as post:
            post.return_value = self._mock_paperless()
            scan_app._create_paperless_metadata_object("tags", "New tag", "owner")
        self.assertEqual(post.call_args.kwargs["json"], {"name": "New tag"})

    def test_storage_path_rejects_missing_template_before_paperless_call(self):
        with patch("app.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "template is required"):
                scan_app._create_paperless_metadata_object(
                    "storage_paths", "My folder", "owner",
                )
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
