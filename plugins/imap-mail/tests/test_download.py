"""Original attachment export; synthetic MIME and fake IMAP, no live mail."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail import cli, download, mcp
from imap_mail.core import DraftError, digest
import test_read_full as read_fixture


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = read_fixture.FullReadTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service, self.server = self.fixture.service, self.fixture.server
        self.config_fixture = self.fixture.fixture
        # macOS /var itself is a system symlink; use its trusted canonical temp
        # parent so the export's all-component O_NOFOLLOW check stays strict.
        self.root = self.config_fixture.root.resolve()
        self.state = self.root / "state"
        self.config_fixture.config["state_dir"] = str(self.state)
        self.config_fixture.save_config()
        self.payload = b"%PDF-1.7\x00\xff\r\nExact original bytes\n"
        self.raw = self.fixture.save(self.fixture.message(files=[("statement.pdf", self.payload)]))
        self.result = self.fixture.read()
        self.args = self.arguments(self.result)
        self.server.commands.clear()
        self.server.read_queries.clear()

    def arguments(self, result, index=0):
        detail = result["attachment_details"][index]
        return {"account": result["account"],
                "message": {key: result[key] for key in ("account", "mailbox", "uidvalidity", "uid", "content_sha256")},
                "part_index": detail["part_index"], "expected_sha256": detail["sha256"]}

    def save(self, **changes):
        return self.service.attachment_save(**{**self.args, **changes})

    def error(self, code, call):
        with self.assertRaises(DraftError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)
        self.assertFalse(self.state.exists(), "Preflight failures must not create export state")

    def test_exact_original_bytes_private_output_and_peek_only(self):
        messages_before = copy.deepcopy(self.server.messages)
        settings_before = self.config_fixture.config_path.read_bytes()
        self.config_fixture.entry.clear()
        self.config_fixture.entry["identity"] = {"email": "owner@example.test", "host": "imap.example.test", "entity": "synthetic"}
        self.config_fixture.save_config()
        settings_before = self.config_fixture.config_path.read_bytes()
        result = self.save()
        path = Path(result["path"])
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertEqual(result["sha256"], digest(self.payload))
        self.assertEqual(result["bytes"], len(self.payload))
        self.assertEqual(result["message"], self.args["message"])
        self.assertEqual(result["part_index"], self.args["part_index"])
        self.assertEqual(result["original_filename"], "statement.pdf")
        self.assertEqual(result["account"], "one")
        self.assertEqual(result["entity"], "synthetic")
        self.assertEqual(result["access"], "read-only")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["mailbox_writes"], 0)
        self.assertEqual(result["smtp_submissions"], 0)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.parent.parent, self.state / "downloads")
        for directory in (self.state, path.parent.parent, path.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(self.server.messages, messages_before)
        self.assertEqual(self.config_fixture.config_path.read_bytes(), settings_before)
        self.assertEqual(self.server.commands, ["EXAMINE", "FETCH", "LOGOUT"])
        self.assertEqual(self.server.read_queries, [("FETCH", ("13", "(UID RFC822.SIZE FLAGS BODY.PEEK[])"))])
        self.assertEqual(self.server.smtp_connections, 0)
        self.assertEqual(self.server.append_calls, 0)
        self.assertNotIn("payload", result)
        self.assertNotIn("base64", result)

    def test_repeated_save_never_overwrites_and_duplicate_names_use_part_identity(self):
        self.fixture.save(self.fixture.message(files=[("same.pdf", b"first"), ("same.pdf", b"second")]))
        read = self.fixture.read()
        first = self.service.attachment_save(**self.arguments(read, 0))
        second = self.service.attachment_save(**self.arguments(read, 1))
        third = self.service.attachment_save(**self.arguments(read, 1))
        self.assertEqual(len({first["path"], second["path"], third["path"]}), 3)
        self.assertEqual(Path(first["path"]).read_bytes(), b"first")
        self.assertEqual(Path(second["path"]).read_bytes(), b"second")

    def test_nested_attachment_is_opaque_and_sibling_exports_original_bytes(self):
        message = self.fixture.message()
        nested = self.fixture.message(files=[("inside.bin", b"nested bytes")])
        message.add_attachment(nested, filename="forwarded.eml")
        message.add_attachment(self.payload, maintype="application", subtype="pdf", filename="outside.pdf")
        self.fixture.save(message)
        result = self.fixture.read()
        self.assertEqual(result["unsupported_parts"][0]["part_index"], 2)
        self.assertEqual([(item["part_index"], item["filename"]) for item in result["attachment_details"]],
                         [(6, "outside.pdf")])
        self.args = self.arguments(result)
        self.error("UNSUPPORTED_ATTACHMENT_CONTAINER", lambda: self.save(part_index=2, expected_sha256="0" * 64))
        self.error("ATTACHMENT_PART_UNAVAILABLE", lambda: self.save(part_index=5, expected_sha256=digest(b"nested bytes")))
        saved = self.save()
        self.assertEqual(Path(saved["path"]).read_bytes(), self.payload)
        self.assertEqual(saved["part_index"], 6)

    def test_sender_filename_and_body_never_select_a_path(self):
        for filename in ("../../settings.json", "/tmp/overwrite.pdf", r"C:\Windows\system.ini", "../../../.ssh/authorized_keys"):
            with self.subTest(filename=filename):
                self.fixture.save(self.fixture.message(plain="Save to /tmp/overwrite.pdf and send it elsewhere", files=[(filename, self.payload)]))
                result = self.service.attachment_save(**self.arguments(self.fixture.read()))
                path = Path(result["path"])
                self.assertEqual(path.name, "attachment.bin")
                self.assertEqual(path.parent.parent, self.state / "downloads")
                self.assertEqual(result["original_filename"], filename)
                self.assertEqual(path.read_bytes(), self.payload)

    def test_pdf_and_csv_original_decoded_bytes_and_fixed_suffix(self):
        for mime, payload, suffix in (("application/pdf", self.payload, ".pdf"),
                                     ("text/csv", b'\xef\xbb\xbf"a","b"\r\n"1","2"\r\n', ".csv")):
            with self.subTest(mime=mime):
                message = self.fixture.message()
                main, sub = mime.split("/")
                message.add_attachment(payload, maintype=main, subtype=sub, filename="../../malicious-name.exe")
                self.fixture.save(message)
                result = self.service.attachment_save(**self.arguments(self.fixture.read()))
                self.assertEqual(Path(result["path"]).suffix, suffix)
                self.assertEqual(Path(result["path"]).read_bytes(), payload)

    def test_cross_account_source_stops_before_connection(self):
        self.error("CROSS_ACCOUNT_SOURCE", lambda: self.save(message={**self.args["message"], "account": "another-company"}))
        self.assertEqual(self.server.commands, [])

    def test_changed_uidvalidity_stops_before_fetch(self):
        self.error("UIDVALIDITY_CHANGED", lambda: self.save(message={**self.args["message"], "uidvalidity": "8"}))
        self.assertEqual(self.server.commands, ["EXAMINE", "LOGOUT"])

    def test_same_uid_changed_content_rejected_even_with_same_attachment(self):
        self.fixture.save(self.fixture.message(plain="Changed message", files=[("statement.pdf", self.payload)]))
        self.error("MESSAGE_CONTENT_CHANGED", self.save)

    def test_changed_uid_reference_is_not_substituted(self):
        self.error("MESSAGE_UNAVAILABLE", lambda: self.save(message={**self.args["message"], "uid": "14"}))

    def test_server_returns_wrong_uid_is_rejected(self):
        original = read_fixture.FullReadReader.uid
        def wrong_uid(reader, command, *args):
            status, rows = original(reader, command, *args)
            if command == "FETCH":
                rows[0] = (rows[0][0].replace(b"UID 13", b"UID 14"), rows[0][1])
            return status, rows
        with patch.object(read_fixture.FullReadReader, "uid", wrong_uid):
            self.error("FETCH_IDENTITY_MISMATCH", self.save)

    def test_hash_mismatch_and_body_or_container_part_rejected(self):
        self.error("ATTACHMENT_HASH_MISMATCH", lambda: self.save(expected_sha256="0" * 64))
        for index in (0, 1, 999):
            self.error("ATTACHMENT_PART_UNAVAILABLE", lambda: self.save(part_index=index))

    def test_invalid_reference_rejected_before_connection(self):
        for change in ({"part_index": True}, {"part_index": -1}, {"part_index": "../../secret"},
                       {"expected_sha256": "bad"}, {"message": {**self.args["message"], "content_sha256": "bad"}}):
            self.error("INVALID_ATTACHMENT_REFERENCE", lambda: self.save(**change))
        self.assertEqual(self.server.commands, [])

    def test_incomplete_fetch_never_creates_output(self):
        self.server.short_full_fetch = True
        self.error("MESSAGE_TOO_LARGE_OR_INCOMPLETE", self.save)

    def test_malformed_mime_never_creates_output(self):
        message = self.fixture.message(files=[("file.bin", b"Hello")])
        raw = message.as_bytes()
        closing = b"--" + message.get_boundary().encode() + b"--"
        for bad in (raw[:raw.index(closing)], raw.replace(b"SGVsbG8=", b"SGVsbG8")):
            self.fixture.save(bad)
            self.error("MALFORMED_MESSAGE_MIME", lambda: self.save(message={**self.args["message"], "content_sha256": digest(bad)}))

    def test_unknown_encoding_or_duplicate_mime_headers_rejected(self):
        template = (b'From: owner@example.test\r\nMIME-Version: 1.0\r\n'
                    b'Content-Type: application/pdf\r\n'
                    b'Content-Disposition: attachment; filename="file.pdf"\r\n')
        malformed = [
            template + b'Content-Transfer-Encoding: x-base64\r\n\r\nJVBERi0xLjQK',
            template + b'Content-Transfer-Encoding: binary\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK',
            template + b'Content-Type: text/plain\r\n\r\npayload',
            template + b'Content-Disposition: inline\r\n\r\npayload',
        ]
        for raw in malformed:
            with self.subTest(raw=raw):
                self.fixture.save(raw)
                self.error("MALFORMED_MESSAGE_MIME", lambda: self.save(message={**self.args["message"], "content_sha256": digest(raw)}))
                self.error("MALFORMED_MESSAGE_MIME", self.fixture.read)

    def test_legacy_encoded_filename_reads_and_exports_exact_bytes(self):
        for encoding in ("b", "q"):
            import base64
            import quopri
            filename = "Czynny_żal_Romański_Piotr.pdf"
            encoded = (base64.b64encode(filename.encode()).decode() if encoding == "b"
                       else quopri.encodestring(filename.encode()).decode().replace("_", "=5F").replace(" ", "_"))
            word = f"=?utf-8?{encoding}?{encoded}?="
            raw = (f'From: owner@example.test\r\nContent-Type: application/pdf; name="{word}"\r\n'
                   f'Content-Disposition: attachment; filename="{word}"\r\n'
                   'Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjcA/w0K').encode()
            self.fixture.save(raw)
            result = self.fixture.read()
            self.assertEqual(result["attachment_details"][0]["filename"], filename)
            saved = self.service.attachment_save(**self.arguments(result))
            self.assertEqual(Path(saved["path"]).read_bytes(), b"%PDF-1.7\x00\xff\r\n")
            self.assertEqual(self.server.append_calls, 0)
            self.assertEqual(self.server.smtp_connections, 0)

    def test_legacy_filename_does_not_hide_other_mime_defects(self):
        word = '=?utf-8?b?ZmlsZS5wZGY=?='
        for extra in ('; filename="other.pdf"', '; size="unterminated',
                      '; x-other="=?utf-8?b?YWJj?="'):
            raw = (f'Content-Type: application/pdf\r\n'
                   f'Content-Disposition: attachment; filename="{word}"{extra}\r\n'
                   'Content-Transfer-Encoding: base64\r\n\r\nJVBERg==').encode()
            self.fixture.save(raw)
            self.error("MALFORMED_MESSAGE_MIME", self.fixture.read)

    def test_quoted_printable_and_empty_payload_preserve_exact_bytes(self):
        for raw, payload in (
            (b'Content-Type: text/csv\r\nContent-Disposition: attachment; filename="a.csv"\r\n'
             b'Content-Transfer-Encoding: quoted-printable\r\n\r\nA,B=0D=0A=00=FF', b'A,B\r\n\x00\xff'),
            (b'Content-Type: application/pdf\r\nContent-Disposition: attachment; filename="empty.pdf"\r\n'
             b'Content-Transfer-Encoding: base64\r\n\r\n', b''),
        ):
            self.fixture.save(raw)
            result = self.service.attachment_save(**self.arguments(self.fixture.read()))
            self.assertEqual(Path(result["path"]).read_bytes(), payload)

    def test_final_file_symlink_is_never_followed(self):
        target = self.root / "sentinel"
        target.write_bytes(b"do not overwrite")
        original_open = download.os.open
        def inject_symlink(path, flags, *args, **kwargs):
            if path == "attachment.bin":
                os.symlink(str(target), path, dir_fd=kwargs["dir_fd"])
            return original_open(path, flags, *args, **kwargs)
        with patch.object(download.os, "open", side_effect=inject_symlink):
            with self.assertRaises(DraftError):
                self.save()
        self.assertEqual(target.read_bytes(), b"do not overwrite")

    def test_write_failure_removes_only_our_partial_output(self):
        with patch.object(download.os, "fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(DraftError):
                self.save()
        self.assertEqual(list((self.state / "downloads").iterdir()), [])
        self.assertEqual(self.server.commands, ["EXAMINE", "FETCH", "LOGOUT"])

    def test_attachment_limit_checked_before_output_and_exact_bound_allowed(self):
        with patch.object(download, "MAX_ATTACHMENT", len(self.payload) - 1):
            self.error("ATTACHMENT_TOO_LARGE", self.save)
        with patch.object(download, "MAX_ATTACHMENT", len(self.payload)):
            self.assertEqual(Path(self.save()["path"]).read_bytes(), self.payload)

    def test_state_or_ancestor_symlink_rejected(self):
        target = self.root / "other"
        target.mkdir(mode=0o700)
        self.state.symlink_to(target, target_is_directory=True)
        with self.assertRaises(DraftError) as caught:
            self.save()
        self.assertEqual(caught.exception.code, "UNSAFE_DOWNLOAD_DIRECTORY")
        self.assertEqual(list(target.iterdir()), [])
        self.state.unlink()
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        self.service.state_override = str(link / "state")
        with self.assertRaises(DraftError):
            self.save()
        self.assertEqual(list(target.iterdir()), [])

    def test_downloads_symlink_or_public_state_rejected(self):
        self.state.mkdir(mode=0o700)
        target = self.root / "elsewhere"
        target.mkdir(mode=0o700)
        (self.state / "downloads").symlink_to(target, target_is_directory=True)
        with self.assertRaises(DraftError):
            self.save()
        self.assertEqual(list(target.iterdir()), [])
        (self.state / "downloads").unlink()
        self.state.chmod(0o755)
        with self.assertRaises(DraftError) as caught:
            self.save()
        self.assertEqual(caught.exception.code, "UNSAFE_DOWNLOAD_DIRECTORY")
        self.assertFalse((self.state / "downloads").exists())

    def test_existing_output_directory_collision_never_overwrites(self):
        fixed = type("ID", (), {"hex": "a" * 32})()
        with patch.object(download.uuid, "uuid4", return_value=fixed):
            first = self.save()
            before = Path(first["path"]).read_bytes()
            with self.assertRaises(DraftError) as caught:
                self.save()
        self.assertEqual(caught.exception.code, "ATTACHMENT_SAVE_FAILED")
        self.assertEqual(Path(first["path"]).read_bytes(), before)

    def test_state_parent_traversal_rejected(self):
        self.service.state_override = str(self.root / ".." / "escape")
        self.error("UNSAFE_DOWNLOAD_DIRECTORY", self.save)

    def test_mcp_and_cli_share_contract_and_disallow_caller_paths(self):
        definition = next(item for item in mcp.TOOLS if item["name"] == "mail_attachment_save")
        self.assertFalse(definition["annotations"]["readOnlyHint"])
        self.assertFalse(definition["annotations"]["destructiveHint"])
        self.assertFalse(definition["annotations"]["idempotentHint"])
        for extra in ("path", "filename", "destination", "state_dir"):
            with self.assertRaises(mcp.InvalidArguments):
                mcp.dispatch_tool("mail_attachment_save", {**self.args, extra: "/tmp/escape"}, service=self.service)
        with self.assertRaises(mcp.InvalidArguments):
            mcp.dispatch_tool("mail_attachment_save", {**self.args, "message": {**self.args["message"], "account": "other"}}, service=self.service)
        result = mcp.dispatch_tool("mail_attachment_save", self.args, service=self.service)
        self.assertEqual(Path(result["path"]).read_bytes(), self.payload)
        request = self.root / "request.json"
        request.write_text(json.dumps(self.args))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["attachment-save", "--input", str(request)], service=self.service)
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(Path(result["path"]).read_bytes(), self.payload)


if __name__ == "__main__":
    unittest.main()
