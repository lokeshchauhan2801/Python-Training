"""Strip all non-docstring comments from the agentic-flow source tree.

Removes:
  * Python (*.py): every ``#`` comment (full-line and trailing inline). Python
    docstrings (module/class/function triple-quoted string literals) are kept;
    a ``#`` inside a string/f-string/regex is never treated as a comment. A
    line-1 shebang is preserved.
  * JavaScript/JSX (*.js, *.jsx): ``//`` line and ``/* ... */`` block comments
    (including ``{/* ... */}`` JSX comments), never inside string/template
    literals or regex literals.
  * CSS (*.css): ``/* ... */`` comments; ``#`` (hex colors) is never a comment.
  * HTML (*.html): ``<!-- ... -->`` and Django ``{# ... #}`` comments. Django
    ``{% ... %}`` and ``{{ ... }}`` tags are kept. Embedded ``<style>`` bodies
    run through the CSS scanner and ``<script>`` bodies through the JS scanner.

Correctness: code behavior is preserved exactly. For Python the apply path
asserts the parsed AST is byte-identical before and after; on mismatch the file
is NOT written. Runs of 3+ blank lines are collapsed to 2 and trailing
whitespace is stripped per line.

Standard library only. CLI::

    python3 tools/strip_comments.py              # apply to all target files
    python3 tools/strip_comments.py --check      # dry run, list would-change
    python3 tools/strip_comments.py --only PATH  # process a single file
"""

import argparse
import ast
import io
import pathlib
import re
import tokenize

PRUNE_DIRS = {
    ".git",
    ".worktrees",
    "node_modules",
    "venv",
    "__pycache__",
    "staticfiles",
    "media",
    "chroma_data",
    "migrations",
    ".agents",
}

SKIP_FILENAMES = {"package-lock.json"}

TARGET_SUFFIXES = {".py", ".js", ".jsx", ".html", ".css"}


def iter_target_files(root):
    """Yield target files under ``root`` honoring the exclusion set."""
    root = pathlib.Path(root)
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir()):
            if entry.is_dir():
                if entry.name in PRUNE_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file():
                if entry.name in SKIP_FILENAMES:
                    continue
                if entry.suffix in TARGET_SUFFIXES:
                    yield entry


def collapse_blank_lines(text):
    """Strip trailing whitespace per line and cap blank runs at 2."""
    lines = [line.rstrip() for line in text.split("\n")]
    out = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run > 2:
                continue
        else:
            blank_run = 0
        out.append(line)
    return "\n".join(out)


def strip_python(src):
    """Remove every ``#`` comment token, keeping strings/docstrings intact.

    Rebuilds source line by line: a full-line comment line is dropped, a
    trailing comment is cut from the comment token's start column to EOL. A
    line-1 shebang is preserved. Raises on tokenize failure so the caller can
    SKIP the file.
    """
    shebang = None
    if src.startswith("#!"):
        nl = src.find("\n")
        if nl == -1:
            return src
        shebang = src[: nl + 1]
        body = src[nl + 1 :]
    else:
        body = src

    readline = io.StringIO(body).readline
    tokens = list(tokenize.generate_tokens(readline))

    comments_by_line = {}
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            start_row, start_col = tok.start
            comments_by_line.setdefault(start_row, []).append(start_col)

    lines = body.split("\n")
    out_lines = []
    for idx, line in enumerate(lines, start=1):
        if idx in comments_by_line:
            cut = min(comments_by_line[idx])
            before = line[:cut]
            if before.strip() == "":
                if before == "":
                    continue
                out_lines.append("")
                continue
            out_lines.append(before.rstrip())
        else:
            out_lines.append(line)
    result = "\n".join(out_lines)
    if shebang is not None:
        result = shebang + result
    return collapse_blank_lines(result)


_JS_PREV_REGEX_OK = re.compile(r"[\w$)\].]$")


def strip_js(src):
    """Remove JS/JSX ``//`` and ``/* */`` comments via a char state machine."""
    out = []
    i = 0
    n = len(src)
    last_significant = ""
    while i < n:
        c = src[i]
        two = src[i : i + 2]

        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                ch = src[i]
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
                i += 1
                if ch == quote:
                    break
            last_significant = quote
            continue

        if c == "`":
            out.append(c)
            i += 1
            while i < n:
                ch = src[i]
                if ch == "\\" and i + 1 < n:
                    out.append(ch)
                    out.append(src[i + 1])
                    i += 2
                    continue
                if ch == "`":
                    out.append(ch)
                    i += 1
                    break
                if src[i : i + 2] == "${":
                    out.append("${")
                    i += 2
                    depth = 1
                    inner = _scan_template_expr(src, i, out)
                    i = inner
                    continue
                out.append(ch)
                i += 1
            last_significant = "`"
            continue

        if two == "//":
            while i < n and src[i] != "\n":
                i += 1
            continue

        if two == "/*":
            i += 2
            while i < n and src[i : i + 2] != "*/":
                i += 1
            i += 2
            continue

        if c == "/":
            if _regex_allowed(last_significant):
                out.append(c)
                i += 1
                in_class = False
                while i < n:
                    ch = src[i]
                    out.append(ch)
                    if ch == "\\" and i + 1 < n:
                        out.append(src[i + 1])
                        i += 2
                        continue
                    i += 1
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        break
                    elif ch == "\n":
                        break
                while i < n and src[i].isalpha():
                    out.append(src[i])
                    i += 1
                last_significant = "/"
                continue
            out.append(c)
            i += 1
            last_significant = "/"
            continue

        out.append(c)
        if not c.isspace():
            last_significant = c
        i += 1

    return collapse_blank_lines("".join(out))


def _scan_template_expr(src, i, out):
    """Scan a ``${ ... }`` template expression as code, appending to ``out``."""
    n = len(src)
    depth = 1
    last_significant = ""
    while i < n and depth > 0:
        c = src[i]
        two = src[i : i + 2]
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                ch = src[i]
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
                i += 1
                if ch == quote:
                    break
            last_significant = quote
            continue
        if c == "`":
            out.append(c)
            i += 1
            while i < n:
                ch = src[i]
                if ch == "\\" and i + 1 < n:
                    out.append(ch)
                    out.append(src[i + 1])
                    i += 2
                    continue
                if ch == "`":
                    out.append(ch)
                    i += 1
                    break
                if src[i : i + 2] == "${":
                    out.append("${")
                    i += 2
                    i = _scan_template_expr(src, i, out)
                    continue
                out.append(ch)
                i += 1
            last_significant = "`"
            continue
        if two == "//":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if two == "/*":
            i += 2
            while i < n and src[i : i + 2] != "*/":
                i += 1
            i += 2
            continue
        if c == "/" and _regex_allowed(last_significant):
            out.append(c)
            i += 1
            in_class = False
            while i < n:
                ch = src[i]
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
                i += 1
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                elif ch == "\n":
                    break
            while i < n and src[i].isalpha():
                out.append(src[i])
                i += 1
            last_significant = "/"
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                out.append(c)
                i += 1
                break
        out.append(c)
        if not c.isspace():
            last_significant = c
        i += 1
    return i


def _regex_allowed(last_significant):
    """Return True if a ``/`` after ``last_significant`` begins a regex."""
    if last_significant == "":
        return True
    return not _JS_PREV_REGEX_OK.search(last_significant)


def strip_css(src):
    """Remove CSS ``/* */`` comments; ``#`` is never a comment."""
    out = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                ch = src[i]
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
                i += 1
                if ch == quote:
                    break
            continue
        if src[i : i + 2] == "/*":
            i += 2
            while i < n and src[i : i + 2] != "*/":
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return collapse_blank_lines("".join(out))


_SCRIPT_RE = re.compile(r"(<script\b[^>]*>)(.*?)(</script>)", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.IGNORECASE | re.DOTALL)
_SCRIPT_TYPE_RE = re.compile(r'type\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)

_NON_JS_SCRIPT_TYPES = {
    "application/json",
    "application/ld+json",
    "text/template",
    "text/html",
    "text/x-template",
}


def _strip_html_comments(src):
    """Remove ``<!-- -->`` and Django ``{# #}`` comments, keep template tags."""
    out = []
    i = 0
    n = len(src)
    while i < n:
        if src[i : i + 4] == "<!--":
            end = src.find("-->", i + 4)
            if end == -1:
                break
            i = end + 3
            continue
        if src[i : i + 2] == "{#":
            end = src.find("#}", i + 2)
            if end == -1:
                out.append(src[i])
                i += 1
                continue
            i = end + 2
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def strip_html(src):
    """Strip HTML/Django comments plus embedded ``<style>``/``<script>`` ones."""

    def style_sub(m):
        return m.group(1) + strip_css(m.group(2)) + m.group(3)

    def script_sub(m):
        open_tag = m.group(1)
        type_match = _SCRIPT_TYPE_RE.search(open_tag)
        if type_match and type_match.group(1).lower() in _NON_JS_SCRIPT_TYPES:
            return m.group(0)
        return open_tag + strip_js(m.group(2)) + m.group(3)

    src = _STYLE_RE.sub(style_sub, src)
    src = _SCRIPT_RE.sub(script_sub, src)
    src = _strip_html_comments(src)
    return collapse_blank_lines(src)


def process(path):
    """Return (new_text, skipped_reason). skipped_reason is None on success."""
    src = path.read_text(encoding="utf-8")
    suffix = path.suffix
    try:
        if suffix == ".py":
            new = strip_python(src)
            try:
                if ast.dump(ast.parse(src)) != ast.dump(ast.parse(new)):
                    return src, "AST mismatch after strip"
            except SyntaxError as exc:
                return src, "parse error: %s" % exc
            return new, None
        if suffix in (".js", ".jsx"):
            return strip_js(src), None
        if suffix == ".css":
            return strip_css(src), None
        if suffix == ".html":
            return strip_html(src), None
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        return src, "tokenize/parse error: %s" % exc
    return src, "unsupported suffix"


def main():
    parser = argparse.ArgumentParser(description="Strip non-docstring comments.")
    parser.add_argument("--check", action="store_true", help="dry run")
    parser.add_argument("--only", help="process a single file path")
    parser.add_argument("--root", default=".", help="tree root (default: cwd)")
    args = parser.parse_args()

    if args.only:
        files = [pathlib.Path(args.only)]
    else:
        files = list(iter_target_files(args.root))

    changed = []
    skipped = []
    for path in files:
        original = path.read_text(encoding="utf-8")
        new, reason = process(path)
        if reason is not None:
            skipped.append((str(path), reason))
            continue
        if new != original:
            changed.append(str(path))
            if not args.check:
                path.write_text(new, encoding="utf-8")

    print("FILES: %d  CHANGED: %d  SKIPPED: %d" % (len(files), len(changed), len(skipped)))
    if args.check:
        for c in changed:
            print("would change:", c)
    for s, reason in skipped:
        print("SKIPPED:", s, "--", reason)
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
