"""HTML verification uses synthetic MIME only, without accounts or network."""

from email import policy
from email.message import EmailMessage
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from imap_mail.core import HTML_UTF8_META, semantic


class HtmlTextEquivalenceTests(unittest.TestCase):
    def message(self, body, charset="utf-8", attachment=None, disposition=None):
        message = EmailMessage(policy=policy.SMTP)
        for name, value in (
            ("From", "owner@example.test"),
            ("To", "recipient@example.test"),
            ("Subject", "Synthetic HTML verification"),
            ("Date", "Wed, 21 Oct 2026 09:00:00 +0000"),
            ("Message-ID", "<html-equivalence@example.test>"),
        ):
            message[name] = value
        message.set_content("Plain text +0200 and 'Owner' stay exact.")
        message.add_alternative(body, subtype="html", charset=charset)
        if disposition:
            message.get_payload()[-1]["Content-Disposition"] = disposition
        # Seven MIME parts, including three independently verified files.
        for index in range(3):
            content = attachment if attachment is not None and index == 0 else bytes([index, 255, 0, 38])
            subtype = "html" if attachment is not None and index == 0 else "octet-stream"
            maintype = "text" if subtype == "html" else "application"
            message.add_attachment(content, maintype=maintype, subtype=subtype,
                                   filename=f"attachment-{index}.{subtype}")
        return message

    def value(self, body, **kwargs):
        return semantic(self.message(body, **kwargs).as_bytes())

    def assertDifferent(self, original, changed):
        self.assertNotEqual(self.value(original), self.value(changed))

    def test_observed_provider_changes_are_equivalent(self):
        original = "<div>At +0200, &#x27;Owner&#x27; wrote:</div>"
        saved = HTML_UTF8_META + "<div>At &#43;0200, 'Owner' wrote:</div>"
        self.assertEqual(self.value(original), self.value(saved))
        self.assertEqual(len(list(self.message(original).walk())), 7)

    def test_named_decimal_hex_and_literal_text_characters_are_equivalent(self):
        forms = (
            "<p>+ ' &amp; &lt;b&gt; café ©</p>",
            "<p>&#43; &#39; &#38; &#60;b&#62; caf&#233; &#169;</p>",
            "<p>&#x2b; &apos; &#x26; &#x3c;b&#x3e; caf&#xe9; &copy;</p>",
        )
        for body in forms[1:]:
            with self.subTest(body=body):
                self.assertEqual(self.value(forms[0]), self.value(body))

    def test_literal_ampersands_and_split_data_are_equivalent(self):
        for original, changed in (
            ("<p>A & B</p>", "<p>A &amp; B</p>"),
            ("A & B & C", "A &#38; B &#x26; C"),
            ("<p>'Owner' +0200</p>\n<p>&amp;</p>",
             "<p>&#x27;Owner&#39; &#43;0200</p>\n<p>&#38;</p>"),
        ):
            with self.subTest(original=original):
                self.assertEqual(self.value(original), self.value(changed))

    def test_decoding_is_not_recursive(self):
        self.assertDifferent("<p>&amp;#43;0200</p>", "<p>&#43;0200</p>")

    def test_escaped_tags_never_equal_actual_markup(self):
        for tag in ("script", "style", "b", "img"):
            with self.subTest(tag=tag):
                self.assertDifferent(f"<div>&lt;{tag}&gt;alert(1)&lt;/{tag}&gt;</div>",
                                     f"<div><{tag}>alert(1)</{tag}></div>")

    def test_text_changes_are_detected(self):
        for original, changed in (
            ("<div>+0200</div>", "<div>&#43;0100</div>"),
            ("<div>+0200</div>", "<div>&#45;0200</div>"),
            ("<div>&#x27;Owner&#x27;</div>", "<div>'Other'</div>"),
            ("<div>A &amp; B</div>", "<div>A B</div>"),
        ):
            with self.subTest(original=original):
                self.assertDifferent(original, changed)

    def test_links_and_image_sources_remain_exact(self):
        for attribute in ("href", "src"):
            with self.subTest(attribute=attribute):
                self.assertDifferent(f'<a {attribute}="https://example.test/a">A</a>',
                                     f'<a {attribute}="https://example.test/b">A</a>')
                self.assertDifferent(f'<a {attribute}="https://example.test/?a=1&amp;b=2">A</a>',
                                     f'<a {attribute}="https://example.test/?a=1&#38;b=2">A</a>')

    def test_all_attribute_spellings_and_start_tags_remain_exact(self):
        for original, changed in (
            ('<div title="&#x27;Owner&#x27;">Text</div>', '<div title="\'Owner\'">Text</div>'),
            ('<div title="&amp;">Text</div>', '<div title="&#38;">Text</div>'),
            ("<DIV>Text</DIV>", "<div>Text</DIV>"),
            ("<div >Text</div>", "<div>Text</div>"),
            ("<br/>", "<br />"),
        ):
            with self.subTest(original=original):
                self.assertDifferent(original, changed)

    def test_raw_end_tags_retain_case_spacing_and_garbage(self):
        for end in ("</DIV>", "</div >", "</ div>", "</div ignored>", "</div/>", "</>", ""):
            with self.subTest(end=end):
                self.assertDifferent("<div>Text</div>", "<div>Text" + end)

    def test_script_style_and_other_raw_text_entities_remain_exact(self):
        for tag in ("script", "style", "xmp", "iframe", "noembed", "noframes", "plaintext"):
            with self.subTest(tag=tag):
                self.assertDifferent(f"<{tag}>A &amp; B</{tag}>", f"<{tag}>A &#38; B</{tag}>")
        self.assertDifferent('<script>let s = "&lt;img&gt;";</script>',
                             '<script>let s = "<img>";</script>')

    def test_self_closing_raw_text_tags_require_exact_source(self):
        for tag in ("script", "style", "xmp", "iframe", "noembed", "noframes", "plaintext"):
            for start in (f"<{tag}/>", f"<{tag} />", f'<{tag.upper()} data-test="1"/>'):
                for suffix in ("", f"</{tag}>", f"</{tag}><p>After</p>"):
                    with self.subTest(start=start, suffix=suffix):
                        self.assertDifferent(start + "A &amp; B" + suffix,
                                             start + "A &#38; B" + suffix)
                with self.subTest(start=start, context="whole fragment"):
                    self.assertDifferent("<p>A &amp; B</p>" + start + "Raw",
                                         "<p>A &#38; B</p>" + start + "Raw")

    def test_plaintext_keeps_everything_after_its_apparent_close_raw(self):
        for suffix in ("A &amp; B", "<p>A &amp; B</p>", "<script>A &amp; B</script>"):
            with self.subTest(suffix=suffix):
                self.assertDifferent("<plaintext>Raw</plaintext>" + suffix,
                                     "<plaintext>Raw</plaintext>" + suffix.replace("&amp;", "&#38;"))
        self.assertEqual(self.value("<p>A &amp; B</p><plaintext>Raw &amp;</plaintext>After &amp;"),
                         self.value("<p>A &#38; B</p><plaintext>Raw &amp;</plaintext>After &amp;"))

    def test_comments_declarations_processing_and_unknown_markup_remain_exact(self):
        for original, changed in (
            ("<!-- &amp; --><p>A</p>", "<!-- &#38; --><p>A</p>"),
            ("<!DOCTYPE html><p>A</p>", "<!doctype html><p>A</p>"),
            ("<?pi &amp; ?><p>A</p>", "<?pi &#38; ?><p>A</p>"),
            ("<![CDATA[&amp;]]><p>A</p>", "<![CDATA[&#38;]]><p>A</p>"),
            ("<!unknown &amp;><p>A</p>", "<!unknown &#38;><p>A</p>"),
            ("<![unknown &amp;]><p>A</p>", "<![unknown &#38;]><p>A</p>"),
        ):
            with self.subTest(original=original):
                self.assertDifferent(original, changed)

    def test_reordered_removed_or_unmatched_markup_is_detected(self):
        for original, changed in (
            ("<p>A</p><b>B</b>", "<b>B</b><p>A</p>"),
            ("<p>A</p><!-- note -->", "<!-- note --><p>A</p>"),
            ("<p>A</p><!-- note -->", "<p>A</p>"),
            ("<p>A</p><!-- one --><!-- two -->", "<p>A</p><!-- two --><!-- one -->"),
            ("<div>A</span>", "<div>A</div>"),
            ("<div>A</div>", "<div>A</span>"),
            ("<div>A</div>", "<div>A</div"),
            ("<div>A</div>", "<div>A<!-- </div>"),
            ("<p>A &amp; B<", "<p>A &#38; B<"),
        ):
            with self.subTest(original=original):
                self.assertDifferent(original, changed)

    def test_only_one_exact_leading_meta_is_ignored(self):
        body = "<p>A &amp; B</p>"
        equivalent = "<p>A &#38; B</p>"
        self.assertEqual(self.value(body), self.value(HTML_UTF8_META + equivalent))
        for changed in (
            HTML_UTF8_META * 2 + equivalent,
            "\n" + HTML_UTF8_META + equivalent,
            equivalent + HTML_UTF8_META,
            equivalent.replace("<p>", "<p>" + HTML_UTF8_META),
            '<meta http-equiv="refresh" content="0;url=https://other.example.test">' + equivalent,
            HTML_UTF8_META + '<meta http-equiv="refresh" content="0">' + equivalent,
            HTML_UTF8_META[:-1] + ' data-extra="1">' + equivalent,
        ):
            with self.subTest(changed=changed):
                self.assertDifferent(body, changed)

    def test_meta_exception_retains_utf8_charset_guard(self):
        body = "<p>Body</p>"
        self.assertNotEqual(self.value(body, charset="iso-8859-1"),
                            self.value(HTML_UTF8_META + body, charset="iso-8859-1"))

    def test_html_attachments_are_exact_file_bytes(self):
        for changed in (b"<p>A &#38; B</p>", HTML_UTF8_META.encode() + b"<p>A &amp; B</p>"):
            with self.subTest(changed=changed):
                self.assertNotEqual(self.value("<p>Body</p>", attachment=b"<p>A &amp; B</p>"),
                                    self.value("<p>Body</p>", attachment=changed))
        original = self.message("<p>Body</p>", attachment=b"<p>A &amp; B</p>")
        changed = self.message("<p>Body</p>", attachment=b"<p>A &#38; B</p>")
        for message in (original, changed):
            list(message.iter_attachments())[0].set_param("charset", "utf-8")
        self.assertNotEqual(semantic(original.as_bytes()), semantic(changed.as_bytes()))

    def test_explicit_inline_disposition_is_not_normalized(self):
        self.assertNotEqual(self.value("<p>A &amp; B</p>", disposition="inline"),
                            self.value("<p>A &#38; B</p>", disposition="inline"))

    def test_html_without_charset_and_plain_text_stay_exact(self):
        original = self.message("<p>A &amp; B</p>")
        changed = self.message("<p>A &#38; B</p>")
        for message in (original, changed):
            message.get_body(preferencelist=("html",)).del_param("charset")
        self.assertNotEqual(semantic(original.as_bytes()), semantic(changed.as_bytes()))
        original = self.message("<p>Body</p>")
        changed = self.message("<p>Body</p>")
        changed.get_body(preferencelist=("plain",)).set_content("Plain text &#43;0200 and 'Owner' stay exact.")
        self.assertNotEqual(semantic(original.as_bytes()), semantic(changed.as_bytes()))


if __name__ == "__main__":
    unittest.main()
