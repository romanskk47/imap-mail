"""FETCH framing through stdlib imaplib; all server bytes are synthetic."""

import imaplib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail import mcp, read_access
from imap_mail.core import DraftError
from imap_mail.fetch_response import FetchError, fetch_records, requested_fetch
import test_read_full as fixtures


class ScriptedIMAP(imaplib.IMAP4):
    def __init__(self, respond):
        self.respond, self.commands = respond, []
        super().__init__()

    def open(self, host="", port=143, timeout=None):
        self.buffer = io.BytesIO(b"* PREAUTH synthetic account\r\n")

    def read(self, size):
        return self.buffer.read(size)

    def readline(self):
        return self.buffer.readline()

    def shutdown(self):
        pass

    def send(self, data):
        tag, command, *arguments = data.rstrip(b"\r\n").split(b" ", 2)
        self.commands.append((command, arguments))
        if command == b"CAPABILITY":
            response = b"* CAPABILITY IMAP4rev1\r\n"
        elif command == b"EXAMINE":
            response = b"* 2 EXISTS\r\n* OK [UIDVALIDITY 9] validity\r\n"
        elif command == b"UID" and arguments[0].startswith(b"FETCH "):
            response = self.respond(arguments[0])
        elif command == b"LOGOUT":
            response = b"* BYE complete\r\n"
        else:
            raise AssertionError("Unexpected synthetic command")
        self.buffer = io.BytesIO(response + tag + b" OK complete\r\n")


def wire_record(raw, *, uid=13, sequence=1, section="", offset=None, after=True,
                size=None, metadata=None, lowercase=False, quoted=False):
    if metadata is None:
        metadata = (f'UID {uid} RFC822.SIZE {len(raw) if size is None else size} '
                    'FLAGS (\\Draft) INTERNALDATE "21-Oct-2026 10:00:00 +0000"').encode()
    body = f"BODY[{section}]".encode()
    if offset is not None:
        body += f"<{offset}>".encode()
    if lowercase:
        metadata, body = metadata.lower(), body.lower()
    body += b' ""' if quoted else b" {" + str(len(raw)).encode() + b"}\r\n" + raw
    attributes = body + b" " + metadata if after else metadata + b" " + body
    return f"* {sequence} FETCH (".encode() + attributes + b")\r\n"


class FetchWireTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.create_connection", side_effect=AssertionError("No network permitted"))
        guard.start()
        self.addCleanup(guard.stop)
        self.fixture = fixtures.FullReadTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.raw = self.fixture.message(plain="Complete body ) UID 99 FLAGS (hostile)").as_bytes()

    def reader(self, respond, profile=None):
        reader = read_access.Reader.__new__(read_access.Reader)
        reader.profile = profile or self.fixture.fixture.profile
        reader.client = ScriptedIMAP(respond)
        return reader

    def service_read(self, respond):
        self.fixture.service.reader_factory = lambda profile: self.reader(respond, profile)
        return mcp.dispatch_tool("mail_read", {"account": "one", "mailbox": "Drafts",
            "uidvalidity": "9", "uid": "13"}, service=self.fixture.service)

    def records(self, response):
        reader = self.reader(lambda _: response)
        try:
            reader.select("Drafts")
            status, rows = reader.client.uid("FETCH", "13", "(UID FLAGS BODY.PEEK[])")
            self.assertEqual(status, "OK")
            return fetch_records(rows)
        finally:
            reader.close()

    def test_mcp_full_read_accepts_prefix_suffix_and_case_insensitive_metadata(self):
        for after, lowercase in ((False, False), (True, False), (True, True)):
            with self.subTest(after=after, lowercase=lowercase):
                result = self.service_read(lambda _: wire_record(self.raw, after=after, lowercase=lowercase))
                self.assertEqual(result["body"], "Complete body ) UID 99 FLAGS (hostile)\n")
                self.assertTrue(result["mime_complete"])
                parsed = self.records(wire_record(self.raw, after=after, lowercase=lowercase))[0]
                self.assertEqual(parsed.internal_date, "21-oct-2026 10:00:00 +0000" if lowercase
                                 else "21-Oct-2026 10:00:00 +0000")

    def test_reply_fetch_and_legacy_read_accept_suffix_metadata(self):
        reader = self.reader(lambda _: wire_record(self.raw, offset=0))
        try:
            raw, flags = self.fixture.service._raw(reader, "Drafts", "9", "13")
            self.assertEqual(raw, self.raw)
            self.assertEqual(flags, ["\\Draft"])
            result = reader.fetch("13")
            self.assertEqual(result["body"], "Complete body ) UID 99 FLAGS (hostile)\r\n")
            self.assertFalse(result["truncated"])
        finally:
            reader.close()

    def test_header_page_binds_each_complete_record_and_retains_dates(self):
        section = "HEADER.FIELDS (" + " ".join(h.upper() for h in read_access.HEADERS) + ")"
        response = (wire_record(b"Subject: second\r\n\r\n", uid=14, sequence=2, section=section, offset=0) +
                    wire_record(b"Subject: first\r\n\r\n", section=section, offset=0, after=False))
        reader = self.reader(lambda _: response)
        try:
            reader.select("Drafts")
            rows = reader.headers(["13", "14"])
            self.assertEqual([(row["uid"], row["Subject"], row["flags"]) for row in rows],
                             [("13", "first", ["\\Draft"]), ("14", "second", ["\\Draft"])])
            self.assertTrue(all(row["internal_date"] == "21-Oct-2026 10:00:00 +0000" for row in rows))
        finally:
            reader.close()

    def test_eof_literal_and_quoted_empty_work_in_all_three_body_read_paths(self):
        for quoted in (False, True):
            for mode in ("full", "reply", "legacy"):
                with self.subTest(quoted=quoted, mode=mode):
                    def respond(command):
                        if f"<{len(self.raw)}.1>".encode() in command:
                            return wire_record(b"", offset=len(self.raw), metadata=b"UID 13", quoted=quoted)
                        return wire_record(self.raw, size=len(self.raw) + 10, offset=None if mode == "full" else 0)
                    if mode == "full":
                        self.assertTrue(self.service_read(respond)["mime_complete"])
                    else:
                        reader = self.reader(respond)
                        try:
                            if mode == "reply":
                                self.assertEqual(self.fixture.service._raw(reader, "Drafts", "9", "13")[0], self.raw)
                            else:
                                reader.select("Drafts")
                                self.assertFalse(reader.fetch("13")["truncated"])
                        finally:
                            reader.close()

    def test_unsolicited_record_cannot_supply_missing_requested_identity_or_flags(self):
        for fields in (f"RFC822.SIZE {len(self.raw)} FLAGS (\\Draft)".encode(),
                       f"UID 13 RFC822.SIZE {len(self.raw)}".encode()):
            response = wire_record(self.raw, metadata=fields) + b"* 2 FETCH (UID 13 FLAGS (\\Draft))\r\n"
            with self.subTest(fields=fields), self.assertRaises(DraftError) as caught:
                self.service_read(lambda _: response)
            self.assertEqual(caught.exception.code, "FETCH_IDENTITY_MISMATCH")

    def test_wrong_duplicate_and_conflicting_fields_are_not_accepted(self):
        base = f"RFC822.SIZE {len(self.raw)} FLAGS (\\Draft)".encode()
        for fields in (b"UID 14 " + base, b"UID 13 UID 13 " + base, b"UID 13 UID 14 " + base,
                       b"UID 13 " + base + b" FLAGS ()", b"UID 13 " + base + b" RFC822.SIZE 1"):
            with self.subTest(fields=fields), self.assertRaises(DraftError) as caught:
                self.service_read(lambda _: wire_record(self.raw, metadata=fields))
            self.assertEqual(caught.exception.code, "FETCH_IDENTITY_MISMATCH")

    def test_multiple_literals_do_not_select_an_arbitrary_body(self):
        responses = (
            wire_record(self.raw) + wire_record(self.raw, uid=13, sequence=2),
            wire_record(self.raw).removesuffix(b")\r\n") + b" BODY[TEXT] {1}\r\nx)\r\n",
        )
        for response in responses:
            with self.subTest(response=response[:50]), self.assertRaises(DraftError):
                self.service_read(lambda _: response)

    def test_wrong_sections_and_offsets_fail_closed(self):
        for section, offset in (("TEXT", None), ("", 0), ("", 1)):
            with self.subTest(section=section, offset=offset), self.assertRaises(DraftError) as caught:
                self.service_read(lambda _: wire_record(self.raw, section=section, offset=offset))
            self.assertEqual(caught.exception.code, "MESSAGE_TOO_LARGE_OR_INCOMPLETE")

    def test_invalid_or_absent_eof_body_never_confirms_completeness(self):
        for probe in (wire_record(b"", uid=14, offset=len(self.raw)),
                      wire_record(b"", offset=len(self.raw) + 1),
                      wire_record(b"x", offset=len(self.raw)),
                      b"* 1 FETCH (UID 13)\r\n",
                      wire_record(b"", offset=len(self.raw)) + b"* 2 FETCH (UID 14 FLAGS ())\r\n"):
            def respond(command):
                return probe if f"<{len(self.raw)}.1>".encode() in command else wire_record(self.raw, size=1)
            with self.subTest(probe=probe), self.assertRaises(DraftError) as caught:
                self.service_read(respond)
            self.assertEqual(caught.exception.code, "MESSAGE_TOO_LARGE_OR_INCOMPLETE")

    def test_literal_length_and_incomplete_record_are_rejected(self):
        for rows in ([ (b"1 (UID 13 BODY[] {2}", b"x"), b")" ],
                     [ (b"1 (UID 13 BODY[] {1}", b"x") ],
                     [ (b"1 (UID 13 BODY[] {1}", b"x"), b") UID 13 FLAGS ()" ]):
            with self.subTest(rows=rows), self.assertRaises(FetchError):
                fetch_records(rows)

    def test_zero_length_literal_is_distinct_from_absent_body(self):
        empty = requested_fetch(self.records(wire_record(b"", metadata=b"UID 13")), "13", "")
        self.assertEqual(empty.literal, b"")
        with self.assertRaises(FetchError) as caught:
            requested_fetch(self.records(b"* 1 FETCH (UID 13 FLAGS ())\r\n"), "13", "")
        self.assertEqual(caught.exception.code, "MESSAGE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
