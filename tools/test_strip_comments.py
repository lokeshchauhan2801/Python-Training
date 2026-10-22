"""Unit tests for the comment stripper, one group per language."""

import unittest

from tools.strip_comments import (
    collapse_blank_lines,
    strip_css,
    strip_html,
    strip_js,
    strip_python,
)


class PythonTests(unittest.TestCase):
    def test_docstring_kept(self):
        src = '"""Module doc."""\n\n\ndef f():\n    """Func doc."""\n    return 1\n'
        out = strip_python(src)
        self.assertIn('"""Module doc."""', out)
        self.assertIn('"""Func doc."""', out)

    def test_full_line_comment_removed(self):
        src = "# a comment\nx = 1\n"
        self.assertEqual(strip_python(src).strip(), "x = 1")

    def test_trailing_comment_removed(self):
        src = "x = 1  # trailing\n"
        self.assertEqual(strip_python(src).strip(), "x = 1")

    def test_hash_in_string_kept(self):
        src = 's = "not # a comment"\n'
        self.assertIn('"not # a comment"', strip_python(src))

    def test_hash_in_fstring_kept(self):
        src = 'color = f"#{value}"\n'
        self.assertIn('f"#{value}"', strip_python(src))

    def test_hash_in_regex_kept(self):
        src = 'import re\np = re.compile(r"#\\d+")\n'
        self.assertIn('r"#\\d+"', strip_python(src))

    def test_shebang_kept(self):
        src = "#!/usr/bin/env python\n# comment\nx = 1\n"
        out = strip_python(src)
        self.assertTrue(out.startswith("#!/usr/bin/env python"))
        self.assertNotIn("# comment", out)

    def test_noqa_removed(self):
        src = "x = bad()  # noqa: BLE001\n"
        out = strip_python(src)
        self.assertNotIn("noqa", out)
        self.assertIn("x = bad()", out)


class JsTests(unittest.TestCase):
    def test_line_comment_removed(self):
        self.assertNotIn("comment", strip_js("const a = 1; // comment\n"))

    def test_block_comment_removed(self):
        self.assertNotIn("block", strip_js("/* block */ const a = 1;\n"))

    def test_template_literal_slashes_kept(self):
        src = "const u = `/graph/${id}/load/`;\n"
        self.assertIn("`/graph/${id}/load/`", strip_js(src))

    def test_regex_literal_kept(self):
        src = 'const r = /["\\\\]/g;\n'
        self.assertIn('/["\\\\]/g', strip_js(src))

    def test_replace_regex_kept(self):
        src = 'name.replace(/_/g, " ");\n'
        self.assertIn("/_/g", strip_js(src))

    def test_jsx_comment_removed(self):
        src = "return <div>{/* hidden */}</div>;\n"
        out = strip_js(src)
        self.assertNotIn("hidden", out)

    def test_slashes_in_string_kept(self):
        src = 'const s = "http://example.com//x";\n'
        self.assertIn("http://example.com//x", strip_js(src))


class CssTests(unittest.TestCase):
    def test_hex_kept(self):
        src = ":root { --ab-bg: #f8fafc; }\n"
        self.assertIn("#f8fafc", strip_css(src))

    def test_block_comment_removed(self):
        src = "/* a comment */\n.x { color: red; }\n"
        out = strip_css(src)
        self.assertNotIn("a comment", out)
        self.assertIn(".x { color: red; }", out)


class HtmlTests(unittest.TestCase):
    def test_html_comment_removed(self):
        self.assertNotIn("secret", strip_html("<div></div><!-- secret -->\n"))

    def test_django_comment_removed(self):
        self.assertNotIn("hidden", strip_html("{# hidden #}<p>x</p>\n"))

    def test_template_tags_kept(self):
        src = "{% block x %}{{ value }}{% endblock %}\n"
        out = strip_html(src)
        self.assertIn("{% block x %}", out)
        self.assertIn("{{ value }}", out)

    def test_embedded_style_comment_removed(self):
        src = "<style>/* c */ .x { color: #fff; }</style>\n"
        out = strip_html(src)
        self.assertNotIn("/* c */", out)
        self.assertIn("#fff", out)

    def test_embedded_script_comment_removed(self):
        src = "<script>// c\nvar x = 1;</script>\n"
        out = strip_html(src)
        self.assertNotIn("// c", out)
        self.assertIn("var x = 1;", out)

    def test_json_script_kept(self):
        src = '<script type="application/json">{"a": 1}</script>\n'
        out = strip_html(src)
        self.assertIn('{"a": 1}', out)

    def test_script_with_closing_in_string(self):
        src = '<script>var s = "</script>";</script>\n'
        out = strip_html(src)
        self.assertIn("var s =", out)


class BlankLineTests(unittest.TestCase):
    def test_collapse_runs(self):
        self.assertEqual(collapse_blank_lines("a\n\n\n\n\nb"), "a\n\n\nb")

    def test_cap_at_two(self):
        out = collapse_blank_lines("a\n\n\n\nb")
        self.assertEqual(out, "a\n\n\nb")

    def test_trailing_ws_stripped(self):
        self.assertEqual(collapse_blank_lines("a   \nb\t"), "a\nb")


if __name__ == "__main__":
    unittest.main()
