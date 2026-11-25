"""Regression tests for reply formatting defects seen in real November 2026 drafts.

All data is synthetic. The shapes reproduce what was observed read-only in a
real mailbox: duplicated closings, Apple Mail collapsing pre-wrap paragraphs,
plain-text dumps of HTML threads, oversized previews and Python 3.15 breaking
Office attachment Content-Type headers.
"""

import copy
import email
from email import policy
from email.message import EmailMessage
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import _html_semantic_value, semantic
from imap_mail.presentation import compact_review
from imap_mail.rendering import (SMTP_POLICY, html_to_text, sanitize_quote_html, strip_apple_attachment_markers,
                                 signature_overlap, text_to_html)
import test_core as fixtures


SIGNATURE = ("Pozdrawiam,\nPiotr Testowy\nCo-founder\nwww.example.test\n"
             "tel.: +48 000 000 000")

HTML_SOURCE = """<html><head><style>p{color:red}</style><script>alert(1)</script></head>
<body><div dir="ltr"><p>Cześć,</p><p>Zobacz <a href="https://example.test/oferta?id=7">ofertę</a>
i <a href="javascript:alert(1)" onclick="steal()">to</a>.</p>
<pre>def zażółć():
    return "gęślą jaźń"  # wcięcie
</pre>
<ul><li>pierwszy</li><li>drugi</li></ul>
<img src="cid:logo@example.test" alt="logo firmy"><img src="https://tracker.example.test/p.gif">
<blockquote class="gmail_quote">Starsza wiadomość<br>druga linia</blockquote>
</div></body></html>"""


class SignatureOverlapTests(unittest.TestCase):
    def test_closing_and_short_name_are_removed_once(self):
        body, removed, warnings = signature_overlap("Treść.\n\nPozdrawiam,\nPiotr", SIGNATURE)
        self.assertEqual(body, "Treść.")
        self.assertEqual(removed, ["Pozdrawiam,", "Piotr"])
        self.assertEqual(warnings, [])

    def test_closing_alone_and_full_name_are_removed(self):
        for tail in ("Pozdrawiam,", "pozdrawiam", "Pozdrawiam,\nPiotr Testowy", "Pozdrawiam,\nPiotr Testowy\nCo-founder"):
            body, removed, _ = signature_overlap("Treść.\n\n" + tail, SIGNATURE)
            self.assertEqual(body, "Treść.", tail)
            self.assertTrue(removed)

    def test_partial_word_and_unanchored_lines_are_kept(self):
        for text in ("Treść.\n\nPozdrawiam,\nPio", "Treść.\n\nPiotr", "Pozdrawiam,"):
            self.assertEqual(signature_overlap(text, SIGNATURE)[0], text)

    def test_different_closing_is_reported_not_rewritten(self):
        text = "Treść.\n\nDzięki i pozdrawiam,\nPiotr"
        body, removed, warnings = signature_overlap(text, SIGNATURE)
        self.assertEqual((body, removed), (text, []))
        self.assertEqual(warnings, ["SIGNATURE_CLOSING_DUPLICATE_POSSIBLE"])

    def test_name_first_signature_keeps_team_closing(self):
        body, removed, _ = signature_overlap("Text.\n\nViele Grüße\nPiotr Testowy", "Piotr Testowy\nExample GmbH")
        self.assertEqual((body, removed), ("Text.\n\nViele Grüße", ["Piotr Testowy"]))


class HtmlToTextTests(unittest.TestCase):
    def test_structure_links_code_and_quotes_survive(self):
        text = html_to_text(HTML_SOURCE)
        self.assertIn("Cześć,\n\nZobacz ofertę <https://example.test/oferta?id=7>", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("color:red", text)
        self.assertIn('def zażółć():\n    return "gęślą jaźń"  # wcięcie', text)
        self.assertIn("- pierwszy\n- drugi", text)
        self.assertIn("[image: logo firmy]", text)
        self.assertIn("> Starsza wiadomość\n> druga linia", text)

    def test_same_text_links_and_mailto_are_not_repeated(self):
        text = html_to_text('<a href="https://www.example.test/">www.example.test</a> '
                            '<a href="mailto:a@example.test">a@example.test</a> '
                            '<a href="mailto:b@example.test">Biuro</a>')
        self.assertEqual(text, "www.example.test a@example.test Biuro <b@example.test>")


class TextToHtmlTests(unittest.TestCase):
    def test_lines_are_blocks_not_css_whitespace(self):
        value = text_to_html("Hej Kuba,\n\ndzięki <b>!\n    kod()")
        self.assertEqual(value, '<div>Hej Kuba,</div><div><br></div><div>dzięki &lt;b&gt;!</div>'
                                '<div style="white-space:pre-wrap">    kod()</div>')


class QuoteSanitizerTests(unittest.TestCase):
    def test_allowlist_keeps_reading_structure_only(self):
        value = sanitize_quote_html(HTML_SOURCE)
        self.assertIn('<a href="https://example.test/oferta?id=7">ofertę</a>', value)
        self.assertIn('<pre>def zażółć():\n    return "gęślą jaźń"  # wcięcie', value)
        for forbidden in ("script", "javascript", "onclick", "<style", "cid:", "tracker", "<img", "class="):
            self.assertNotIn(forbidden, value)
        self.assertIn("[image: logo firmy]", value)
        self.assertNotEqual(_html_semantic_value(value), [("raw", value)])

    def test_unbalanced_markup_is_closed_and_empty_input_rejected(self):
        self.assertEqual(sanitize_quote_html("<div><b>x</div>"), "<div><b>x</b></div>")
        self.assertIsNone(sanitize_quote_html("<script>only()</script>"))
        self.assertIsNone(sanitize_quote_html(""))


class MimeSafePolicyTests(unittest.TestCase):
    def test_office_content_types_are_never_encoded_words(self):
        message = EmailMessage(policy=SMTP_POLICY.clone(max_line_length=76))
        message["Subject"] = "Re: Współpraca XTB × Startuj.AI i bardzo długi temat z żółcią, który trzeba zawinąć"
        message.set_content("Treść")
        for subtype in ("vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
            message.add_attachment(b"PK", maintype="application", subtype=subtype,
                                   filename="Umowa szkolenie 2026-11-18 bardzo długa nazwa żółć.docx")
        raw = message.as_bytes()
        self.assertNotIn(b"application/=?", raw)
        parsed = email.message_from_bytes(raw, policy=policy.default)
        types = [part.get_content_type() for part in parsed.iter_attachments()]
        self.assertEqual(types, ["application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"])
        self.assertTrue(all(not part.defects for part in parsed.walk()))
        self.assertEqual(str(parsed["Subject"]), str(message["Subject"]))
        words = re.findall(rb"=\?[^?]+\?[qQbB]\?[^?]*\?=", raw)
        self.assertTrue(words and max(len(word) for word in words) <= 75)
        semantic(raw)


class AppleAttachmentMarkerTests(unittest.TestCase):
    """Apple Mail deletes <img> wrapped in span.Apple-string-attachment when it reopens a draft."""

    def test_only_the_apple_marker_is_removed(self):
        cases = {
            '<span class="Apple-string-attachment"><img src="cid:a@local"></span>': '<span><img src="cid:a@local"></span>',
            '<span class="x Apple-string-attachment y">': '<span class="x y">',
            "<SPAN CLASS=Apple-string-attachment>": "<SPAN>",
            "<span class='apple-string-attachment'>": "<span>",
            '<p class="other">x</p>': '<p class="other">x</p>',
            "<div class=other>": "<div class=other>",
        }
        for source, expected in cases.items():
            self.assertEqual(expected, strip_apple_attachment_markers(source))


class ReplyCompositionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.DraftServiceTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        for name in ("root", "server", "entry", "config", "config_path"):
            setattr(self, name, getattr(fixture, name))
        self.preview, self.apply, self.mime, self.json = fixture.preview, fixture.apply, fixture.mime, fixture.json

    def html_source(self):
        message = EmailMessage(policy=SMTP_POLICY)
        message["From"] = "Jakub <jakub@example.test>"
        message["To"] = "owner@example.test"
        message["Subject"] = "Współpraca XTB × Startuj.AI"
        message["Message-ID"] = "<html-source@example.test>"
        message["Date"] = "Tue, 24 Nov 2026 15:38:02 +0100"
        message.set_content("Cześć,\n\nZobacz ofertę <https://example.test/oferta?id=7>\n\n" + "> stara linia\n" * 400)
        message.add_alternative(HTML_SOURCE, subtype="html")
        self.server.messages["INBOX"]["13"] = message.as_bytes()
        return {"mailbox": "INBOX", "uidvalidity": "7", "uid": "13"}

    def signed(self):
        plain = self.root / "signature.txt"
        plain.write_text(SIGNATURE, encoding="utf-8")
        self.entry["signature"] = {"plain": str(plain)}
        self.json(self.config_path, self.config)

    def test_signature_images_drop_apple_marker_but_keep_cid_images(self):
        plain = self.root / "signature.txt"
        plain.write_text(SIGNATURE, encoding="utf-8")
        rich = self.root / "signature.html"
        rich.write_text('<div>Pozdrawiam,</div><table><tr><td><span class="Apple-string-attachment">'
                        '<img src="cid:avatar@local" width="75"></span></td></tr></table>', encoding="utf-8")
        logo = self.root / "avatar.png"
        logo.write_bytes(b"synthetic image bytes")
        self.entry["signature"] = {"plain": str(plain), "html": str(rich), "inline": {"avatar@local": str(logo)}}
        self.json(self.config_path, self.config)
        preview = self.preview(body_text="Treść testowa.")
        html = self.mime(preview).get_body(preferencelist=("html",)).get_content()
        self.assertNotIn("Apple-string-attachment", html)
        self.assertIn('<span><img src="cid:avatar@local" width="75"></span>', html)
        self.assertTrue(any(str(p.get("Content-ID")) == "<avatar@local>" for p in self.mime(preview).walk()))
        self.assertEqual("verified", self.apply(preview)["status"])

    def test_reply_keeps_one_signature_paragraphs_links_and_code(self):
        self.signed()
        body = "Hej Kuba,\n\ndzięki za poprawki!\nDruga linia.\n\nPozdrawiam,\nPiotr"
        preview = self.preview(to=None, subject=None, reply=self.html_source(), body_text=body)
        self.assertEqual(preview["signature_overlap_removed"], ["Pozdrawiam,", "Piotr"])
        rendered = preview["rendered_body_text"]
        self.assertEqual(rendered.count("Pozdrawiam,"), 1)
        message = self.mime(preview)
        self.assertEqual(str(message["Subject"]), "Re: Współpraca XTB × Startuj.AI")
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("<div>Hej Kuba,</div><div><br></div><div>dzięki za poprawki!</div><div>Druga linia.</div>", html)
        self.assertNotIn('<div style="white-space:pre-wrap">Hej', html)
        self.assertEqual(html.count("Pozdrawiam,"), 1)
        self.assertIn('<blockquote type="cite"', html)
        self.assertIn('<a href="https://example.test/oferta?id=7">ofertę</a>', html)
        self.assertIn("<pre>def zażółć():", html)
        self.assertNotIn("javascript", html)
        self.assertNotIn("<script", html)
        self.assertEqual(preview["quote"]["format"], "html")
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("> Zobacz ofertę <https://example.test/oferta?id=7>", plain)
        self.assertEqual(self.apply(preview)["status"], "verified")

    def test_html_only_source_quotes_readable_plain_text(self):
        message = EmailMessage(policy=SMTP_POLICY)
        message["From"] = "Titan <titan@example.test>"
        message["To"] = "owner@example.test"
        message["Subject"] = "HTML only"
        message["Message-ID"] = "<html-only@example.test>"
        message.set_content(HTML_SOURCE, subtype="html")
        self.server.messages["INBOX"]["13"] = message.as_bytes()
        preview = self.preview(to=None, subject=None,
                               reply={"mailbox": "INBOX", "uidvalidity": "7", "uid": "13"})
        self.assertIn("> Zobacz ofertę <https://example.test/oferta?id=7>", preview["rendered_body_text"])
        self.assertIn('>     return "gęślą jaźń"  # wcięcie', preview["rendered_body_text"])

    def test_compact_preview_summarizes_only_the_quoted_original(self):
        self.signed()
        preview = self.preview(to=None, subject=None, reply=self.html_source(), body_text="Krótka odpowiedź.")
        compact = compact_review(copy.deepcopy(preview))
        self.assertTrue(compact["body_text"].startswith("Krótka odpowiedź.\n\nPozdrawiam,\nPiotr Testowy"))
        self.assertTrue(compact["body_text"].endswith("tel.: +48 000 000 000"))
        quoted = compact["quoted_original"]
        self.assertTrue(quoted["elided"])
        self.assertEqual(quoted["sha256"], preview["quote"]["sha256"])
        self.assertLessEqual(len(quoted["excerpt"].splitlines()), 15)
        self.assertEqual(compact["review_completeness"]["body_truncated"], "quoted_original_only")
        self.assertLess(len(str(compact)), len(str(preview)) / 2)
        self.assertEqual(compact["preview_hash"], preview["preview_hash"])

    def test_signature_warning_is_part_of_reviewed_preview(self):
        self.signed()
        preview = self.preview(body_text="Treść.\n\nDzięki i pozdrawiam,\nPiotr")
        self.assertEqual(preview["warnings"], ["SIGNATURE_CLOSING_DUPLICATE_POSSIBLE"])
        self.assertNotIn("signature_overlap_removed", preview)


if __name__ == "__main__":
    unittest.main()
