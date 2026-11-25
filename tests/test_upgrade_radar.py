"""Tests for upgrade-radar. Standard library only, no network: python -m unittest discover -s tests"""
import io
import json
import sys
import tarfile
import tempfile
import textwrap
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import upgrade_radar as ur  # noqa: E402

OLD = {
    "demo/__init__.py": "from .client import Client, connect\nfrom .util import *\n__version__ = '1.0'\n",
    "demo/client.py": textwrap.dedent("""
        class Client:
            def __init__(self, url, timeout=10):
                pass
            def get(self, path, retries=3):
                pass
            @property
            def closed(self):
                return False
        def connect(url, timeout=10, verify=True):
            return Client(url, timeout)
        def _private(x):
            pass
    """),
    "demo/util.py": "__all__ = ['slugify', 'chunk']\ndef slugify(text, sep='-'):\n    pass\n"
                    "def chunk(seq, size):\n    pass\ndef helper():\n    pass\n",
    "demo/legacy.py": "def old_api():\n    pass\n",
    "demo/_internal.py": "def secret():\n    pass\n",
}
NEW = {
    "demo/__init__.py": "from .client import Client, connect\nfrom .util import *\n__version__ = '2.0'\n",
    "demo/client.py": textwrap.dedent("""
        class Client:
            def __init__(self, url, *, timeout=10, token):
                pass
            def get(self, path, retries=3):
                pass
        def connect(url, verify=True, timeout=10):
            return Client(url, timeout=timeout, token='')
        def connect_async(url):
            pass
    """),
    "demo/util.py": "__all__ = ['slugify']\ndef slugify(text, separator='-'):\n    pass\n"
                    "def chunk(seq, size):\n    pass\n",
}
USER = {
    "app.py": textwrap.dedent("""
        import demo
        from demo import connect
        from demo.util import slugify, chunk
        from demo.legacy import old_api

        c = connect("https://x", 5)
        ok = connect("https://x", timeout=5)
        s = slugify("a b", sep="_")
        t = slugify("a b")
        client = demo.Client("https://x", 3)
        opts = {}
        d = demo.connect("u", **opts)
        u = demo.chunk([1, 2], 1)
    """),
    "other.py": "import demo.legacy as lg\nlg.old_api()\nimport json\njson.dumps({})\n",
    "broken.py": "def f(:\n",
}


def as_mods(files):
    return {ur._module_name(k): v for k, v in files.items()}


def write_tree(base: Path, files: dict):
    for rel, text in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def make_wheel(path: Path, files: dict):
    with zipfile.ZipFile(path, "w") as z:
        for rel, text in files.items():
            z.writestr(rel, text)
        z.writestr("demo-1.0.dist-info/METADATA", "Name: demo\n")


def make_sdist(path: Path, files: dict, top="demo-2.0"):
    with tarfile.open(path, "w:gz") as t:
        for rel, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/src/{rel}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        data = b"def test_x(): pass\n"
        info = tarfile.TarInfo(f"{top}/tests/test_demo.py")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.old = self.d / "demo-1.0-py3-none-any.whl"
        self.new = self.d / "demo-2.0.tar.gz"
        make_wheel(self.old, OLD)
        make_sdist(self.new, NEW)
        self.repo = self.d / "repo"
        write_tree(self.repo, USER)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self):
        return ur.build_report(str(self.old), str(self.new), str(self.repo))


class TestLoading(Base):
    def test_wheel_and_sdist_modules(self):
        old, _ = ur.load_sources(str(self.old))
        new, _ = ur.load_sources(str(self.new))
        self.assertIn("demo.client", old)
        self.assertIn("demo", new)
        self.assertNotIn("tests.test_demo", new)          # sdist tests are not API
        self.assertFalse(any("dist-info" in m for m in old))

    def test_directory_with_src_layout(self):
        d = self.d / "proj"
        write_tree(d / "src", NEW)
        mods, info = ur.load_sources(str(d))
        self.assertEqual(info["kind"], "directory")
        self.assertIn("demo.util", mods)

    def test_bad_input(self):
        with self.assertRaises(ur.InputError):
            ur.load_sources(str(self.d / "missing.whl"))
        (self.d / "x.txt").write_text("x")
        with self.assertRaises(ur.InputError):
            ur.load_sources(str(self.d / "x.txt"))
        with self.assertRaises(ur.InputError):
            ur.fetch_pypi("requests")                     # no version: refused before any network

    def test_path_traversal_entries_are_ignored(self):
        evil = self.d / "evil.whl"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../outside.py", "def x(): pass\n")
            z.writestr("pkg/__init__.py", "def ok(): pass\n")
        mods, _ = ur.load_sources(str(evil))
        self.assertEqual(set(mods), {"pkg"})
        self.assertFalse((self.d.parent / "outside.py").exists())


class TestApi(Base):
    def api(self, files):
        return ur.extract_api(as_mods(files))["symbols"]

    def test_public_symbols(self):
        s = self.api(OLD)
        self.assertEqual(s["demo.connect"]["kind"], "function")
        self.assertEqual(s["demo.Client"]["kind"], "class")
        self.assertEqual([p["name"] for p in s["demo.Client"]["params"]], ["url", "timeout"])  # self dropped
        self.assertEqual(s["demo.client.Client.closed"]["kind"], "attribute")
        self.assertNotIn("demo.client._private", s)
        self.assertNotIn("demo._internal", s)
        self.assertNotIn("demo._internal.secret", s)

    def test_all_and_star_import(self):
        s = self.api(OLD)
        self.assertIn("demo.slugify", s)                 # through `from .util import *` and __all__
        self.assertNotIn("demo.helper", s)               # not in __all__, so not star-exported
        self.assertIn("demo.util.helper", s)             # still importable from its own module

    def test_external_import_is_not_api(self):
        s = self.api({"m/__init__.py": "from os import path\nimport json as j\ndef f(): pass\n"})
        self.assertNotIn("m.path", s)
        self.assertIn("m.f", s)

    def test_syntax_error_is_a_warning(self):
        api = ur.extract_api(as_mods({"m/__init__.py": "def f(:\n", "m/ok.py": "def g(): pass\n"}))
        self.assertTrue(api["warnings"])
        self.assertIn("m.ok.g", api["symbols"])


class TestCompare(unittest.TestCase):
    def sig(self, src):
        fn = ur.ast.parse(src).body[0]
        return ur._sig(fn.args, False)

    def test_rules(self):
        c = ur.compare_params
        self.assertEqual(c(self.sig("def f(a, b=1): pass"), self.sig("def f(a, b=1, c=2): pass")), [])
        self.assertIn("new required parameter 'c'", c(self.sig("def f(a): pass"), self.sig("def f(a, c): pass")))
        self.assertIn("parameter 'b' lost its default (now required)",
                      c(self.sig("def f(a, b=1): pass"), self.sig("def f(a, b): pass")))
        self.assertIn("'b' became keyword-only", c(self.sig("def f(a, b=1): pass"), self.sig("def f(a, *, b=1): pass")))
        self.assertTrue(any("positional parameter 2" in x for x in
                            c(self.sig("def f(a, b=1, c=2): pass"), self.sig("def f(a, c=2, b=1): pass"))))
        self.assertIn("**kwargs no longer accepted", c(self.sig("def f(**kw): pass"), self.sig("def f(): pass")))
        self.assertEqual(c(self.sig("def f(a, b): pass"), self.sig("def f(a, b, **kw): pass")), [])

    def test_check_call(self):
        old, new = self.sig("def f(url, timeout=10, verify=True): pass"), self.sig("def f(url, verify=True, timeout=10): pass")
        call = lambda s: ur.ast.parse(s).body[0].value   # noqa: E731
        self.assertTrue(ur.check_call(call("f('u', 5)"), old, new))       # 5 now means verify
        self.assertEqual(ur.check_call(call("f('u', timeout=5)"), old, new), [])
        self.assertEqual(ur.check_call(call("f('u')"), old, new), [])
        self.assertEqual(ur.check_call(call("f(*a)"), old, new), [])      # cannot tell


class TestReport(Base):
    def test_api_changes(self):
        r = self.report()
        ch = {c["symbol"]: c for c in r["changes"]}
        self.assertEqual(ch["demo.legacy"]["change"], "removed")
        self.assertNotIn("demo.legacy.old_api", ch)       # covered by the module removal
        self.assertEqual(ch["demo.client.connect"]["change"], "signature")
        self.assertIn("new required parameter 'token'", ch["demo.Client"]["details"])
        self.assertEqual(ch["demo.chunk"]["change"], "removed")  # dropped from __all__ -> not star-exported
        self.assertNotIn("demo.util.chunk", ch)          # still defined in demo.util
        self.assertEqual(ch["demo.client.Client.closed"]["change"], "removed")
        self.assertFalse(ch["demo.client.connect_async"]["breaking"])
        self.assertNotIn("demo.__version__", ch)

    def test_call_sites(self):
        r = self.report()
        hits = {(h["file"], h["line"]): h for h in r["hits"]}
        self.assertIn(("app.py", 5), hits)                # from demo.legacy import old_api
        self.assertEqual(hits[("app.py", 5)]["certainty"], "breaks")
        self.assertIn(("app.py", 7), hits)                # connect("https://x", 5): 5 binds to verify
        self.assertNotIn(("app.py", 8), hits)             # keyword timeout is still fine
        self.assertIn(("app.py", 9), hits)                # sep= renamed to separator
        self.assertNotIn(("app.py", 10), hits)            # positional text is fine
        self.assertIn(("app.py", 11), hits)               # Client: 3 positional, token required
        self.assertEqual(hits[("app.py", 13)]["certainty"], "review")   # **opts
        self.assertIn(("other.py", 1), hits)              # import demo.legacy as lg
        self.assertTrue(any("broken.py" in w for w in r["warnings"]))
        self.assertEqual(ur.exit_code(r), ur.EXIT_BREAKS)
        self.assertGreaterEqual(r["summary"]["breaks"], 5)

    def test_dropped_from_all(self):
        r = self.report()
        lines = {(h["file"], h["line"]) for h in r["hits"]}
        self.assertNotIn(("app.py", 4), lines)            # from demo.util import chunk still works
        self.assertIn(("app.py", 14), lines)              # demo.chunk came from `import *`, now gone

    def test_no_changes(self):
        r = ur.build_report(str(self.old), str(self.old), str(self.repo))
        self.assertEqual(r["api"]["breaking"], 0)
        self.assertEqual(r["hits"], [])
        self.assertEqual(ur.exit_code(r), ur.EXIT_OK)

    def test_cli_json_and_text(self):
        out = self.d / "report.json"
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ur.main(["check", str(self.old), str(self.new), "--repo", str(self.repo), "--out", str(out)])
        self.assertEqual(code, ur.EXIT_BREAKS)
        self.assertIn("call site(s) break", buf.getvalue())
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["tool"], "upgrade-radar")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ur.main(["diff", str(self.old), str(self.new), "--json"])
        self.assertEqual(code, ur.EXIT_BREAKS)
        self.assertNotIn("hits", json.loads(buf.getvalue()))

    def test_readme_example_is_real_output(self):
        """Every line of the README example (after the command) must appear in the real output."""
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        block = readme.split("```console", 1)[1].split("```", 1)[0].strip().splitlines()[1:]
        real = [" ".join(l.split()) for l in ur.format_text(self.report()).splitlines()]
        for line in block:
            self.assertIn(" ".join(line.split()), real, "\n".join(real))

    def test_cli_bad_input(self):
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ur.main(["diff", "nope.whl", str(self.new)]), ur.EXIT_BAD)


class TextSummaryTest(unittest.TestCase):
    """Bulk removals of module internals are summarised in the text; JSON keeps every symbol.
    Seen on packaging 21.3 -> 22.0: 54 removals, 48 of them pyparsing grammar constants."""

    def _report(self, old, new, user):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for name, files in (("old", old), ("new", new), ("repo", user)):
                for rel, src in files.items():
                    f = d / name / rel
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(src, encoding="utf-8")
            return ur.build_report(str(d / "old"), str(d / "new"), str(d / "repo"))

    def test_internal_names_are_summarised_but_kept(self):
        consts = "".join(f"C{i} = {i}\n" for i in range(8))
        old = {"lib/__init__.py": "",
               "lib/grammar.py": consts + "def parse(s):\n    pass\n",
               "lib/api.py": "__all__ = ['run']\ndef run():\n    pass\ndef helper():\n    pass\n"}
        new = {"lib/__init__.py": "",
               "lib/grammar.py": "def parse(s):\n    pass\n",
               "lib/api.py": "__all__ = ['run']\ndef run():\n    pass\n"}
        user = {"app.py": "from lib.grammar import C3\nfrom lib.api import helper\n"}
        r = self._report(old, new, user)
        removed = {c["symbol"] for c in r["changes"] if c["change"] == "removed"}
        self.assertIn("lib.grammar.C7", removed)                      # JSON keeps all of them
        self.assertTrue(next(c for c in r["changes"] if c["symbol"] == "lib.api.helper")["outside_all"])
        self.assertEqual(r["api"]["breaking"], 9)                     # the count is not lowered
        text = ur.format_text(r)
        self.assertIn("lib.grammar: 8 module-level names (C0, C1, C2, C3, ...)", text)
        self.assertIn("1 name(s) the old version left out of __all__ (lib.api.helper)", text)
        self.assertNotIn("removed   lib.grammar.C5", text)
        self.assertIn("app.py:1:1", text)                             # the user's own import still breaks
        self.assertIn("app.py:2:1", text)

    def test_few_names_are_listed_one_by_one(self):
        old = {"lib/__init__.py": "A = 1\nB = 2\n"}
        new = {"lib/__init__.py": ""}
        text = ur.format_text(self._report(old, new, {"app.py": "x = 1\n"}))
        self.assertIn("removed   lib.A (name)", text)
        self.assertNotIn("module-level names", text)


if __name__ == "__main__":
    unittest.main()
