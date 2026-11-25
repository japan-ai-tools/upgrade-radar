#!/usr/bin/env python3
"""upgrade-radar: what breaks in *your* code when a Python dependency moves from one version to another.

It reads two versions of a package (wheel, sdist, directory, or pypi:NAME==VERSION), extracts the
public API with the ast module (nothing is imported or executed), diffs it, and then scans your
repository for the call sites that the diff actually breaks.

Python 3.9+ standard library only. Network is used only for the explicit pypi: form.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "0.1.1"
MAX_FILE = 2 * 1024 * 1024          # one .py file
MAX_TOTAL = 200 * 1024 * 1024       # all .py files of one package version
MAX_FILES = 20000
MAX_DOWNLOAD = 60 * 1024 * 1024
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox",
             ".nox", "site-packages", "build", "dist", ".mypy_cache", ".pytest_cache", ".eggs"}

EXIT_OK, EXIT_BREAKS, EXIT_BAD, EXIT_REVIEW = 0, 1, 2, 3


class InputError(Exception):
    pass


# ---------------------------------------------------------------- reading package sources

def _module_name(rel: str) -> str | None:
    """'pkg/sub/mod.py' -> 'pkg.sub.mod'; '__init__' collapses; non-identifiers are skipped."""
    p = PurePosixPath(rel)
    if p.suffix not in (".py", ".pyi"):
        return None
    parts = list(p.with_suffix("").parts)
    if not parts:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(x.isidentifier() for x in parts):
        return None
    return ".".join(parts)


def _strip_layout(paths: list[str], sdist: bool) -> dict[str, str]:
    """Map archive paths to package-relative paths (drop 'name-1.0/' and 'src/')."""
    out = {}
    for path in paths:
        parts = PurePosixPath(path).parts
        if not parts or any(x in ("..",) or x.startswith("/") for x in parts):
            continue
        if sdist and len(parts) > 1:
            parts = parts[1:]
        if parts and parts[0] == "src" and len(parts) > 1:
            parts = parts[1:]
        if parts and (parts[0].endswith(".dist-info") or parts[0].endswith(".data")
                      or parts[0].endswith(".egg-info")):
            continue
        if parts and parts[0] in ("tests", "test", "docs", "examples", "benchmarks"):
            continue
        out[path] = "/".join(parts)
    return out


def _collect(entries) -> dict[str, str]:
    """entries: iterable of (relpath, size, reader). Returns {module_name: source}."""
    mods, total, count = {}, 0, 0
    for rel, size, read in entries:
        name = _module_name(rel)
        if name is None:
            continue
        count += 1
        if count > MAX_FILES:
            raise InputError(f"too many Python files (> {MAX_FILES})")
        if size > MAX_FILE:
            continue
        total += size
        if total > MAX_TOTAL:
            raise InputError("package sources are too large")
        data = read()
        text = data.decode("utf-8", errors="replace")
        # a .py file wins over a .pyi stub only if no stub exists (stubs describe the API better)
        if rel.endswith(".pyi") or name not in mods:
            mods[name] = text
    return mods


def load_sources(spec: str, cache_dir: Path | None = None) -> tuple[dict[str, str], dict]:
    """Return ({module: source}, info) for a wheel/sdist/zip/directory or pypi:NAME==VER."""
    if spec.startswith("pypi:"):
        path, info = fetch_pypi(spec[5:], cache_dir)
        mods, inner = load_sources(str(path))
        info.update({k: v for k, v in inner.items() if k not in info})
        return mods, info
    p = Path(spec)
    if p.is_dir():
        base = p / "src" if (p / "src").is_dir() else p
        files = [f for f in base.rglob("*") if f.is_file() and f.suffix in (".py", ".pyi")
                 and not (set(f.relative_to(base).parts[:-1]) & SKIP_DIRS)]
        rels = {f.relative_to(base).as_posix(): f for f in files}
        mapping = _strip_layout(list(rels), sdist=False)
        entries = [(mapping[r], f.stat().st_size, f.read_bytes) for r, f in rels.items() if r in mapping]
        return _collect(entries), {"source": spec, "kind": "directory"}
    if not p.is_file():
        raise InputError(f"not found: {spec}")
    name = p.name.lower()
    if name.endswith((".whl", ".zip")):
        try:
            zf = zipfile.ZipFile(p)
        except zipfile.BadZipFile as e:
            raise InputError(f"bad zip/wheel: {spec}: {e}")
        infos = [i for i in zf.infolist() if not i.is_dir()]
        sdist = name.endswith(".zip")
        mapping = _strip_layout([i.filename for i in infos], sdist=sdist)
        entries = [(mapping[i.filename], i.file_size, (lambda i=i: zf.read(i)))
                   for i in infos if i.filename in mapping]
        return _collect(entries), {"source": spec, "kind": "wheel" if not sdist else "sdist"}
    if name.endswith((".tar.gz", ".tgz", ".tar")):
        try:
            tf = tarfile.open(p)
        except tarfile.TarError as e:
            raise InputError(f"bad tar archive: {spec}: {e}")
        members = [m for m in tf.getmembers() if m.isfile()]   # links and devices are ignored
        mapping = _strip_layout([m.name for m in members], sdist=True)

        def reader(m):
            return lambda: tf.extractfile(m).read()
        entries = [(mapping[m.name], m.size, reader(m)) for m in members if m.name in mapping]
        return _collect(entries), {"source": spec, "kind": "sdist"}
    raise InputError(f"unsupported input (use .whl, .tar.gz, .zip, a directory or pypi:NAME==VER): {spec}")


def fetch_pypi(req: str, cache_dir: Path | None = None) -> tuple[Path, dict]:
    """Download one release file from PyPI's JSON API and check its sha256 digest."""
    if "==" not in req:
        raise InputError("use pypi:NAME==VERSION")
    pkg, ver = (x.strip() for x in req.split("==", 1))
    if not pkg or not ver or "/" in pkg or "/" in ver:
        raise InputError(f"bad requirement: {req}")
    url = f"https://pypi.org/pypi/{pkg}/{ver}/json"
    headers = {"User-Agent": f"upgrade-radar/{VERSION}"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        meta = json.loads(r.read().decode("utf-8"))
    files = meta.get("urls", [])
    pick = ([f for f in files if f["filename"].endswith("-none-any.whl")]
            or [f for f in files if f.get("packagetype") == "sdist"]
            or [f for f in files if f["filename"].endswith(".whl")])
    if not pick:
        raise InputError(f"no wheel or sdist on PyPI for {pkg}=={ver}")
    f = pick[0]
    if f.get("size", 0) > MAX_DOWNLOAD:
        raise InputError(f"{f['filename']} is larger than {MAX_DOWNLOAD // 1024 // 1024} MB")
    cache = Path(cache_dir or Path.home() / ".cache" / "upgrade-radar")
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / Path(f["filename"]).name
    want = f.get("digests", {}).get("sha256")
    if not (dest.exists() and want and hashlib.sha256(dest.read_bytes()).hexdigest() == want):
        with urllib.request.urlopen(urllib.request.Request(f["url"], headers=headers), timeout=60) as r:
            data = r.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise InputError("download too large")
        got = hashlib.sha256(data).hexdigest()
        if want and got != want:
            raise InputError(f"sha256 mismatch for {f['filename']}")
        dest.write_bytes(data)
    return dest, {"source": f"pypi:{pkg}=={ver}", "file": f["filename"], "sha256": want}


# ---------------------------------------------------------------- extracting the public API

def _sig(args: ast.arguments, drop_first: bool) -> list[dict]:
    pos = [(a, "posonly") for a in getattr(args, "posonlyargs", [])] + [(a, "pos") for a in args.args]
    ndef = len(args.defaults)
    params = []
    for i, (a, kind) in enumerate(pos):
        params.append({"name": a.arg, "kind": kind, "default": i >= len(pos) - ndef})
    if drop_first and params:
        params = params[1:]
    if args.vararg:
        params.append({"name": args.vararg.arg, "kind": "var", "default": True})
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        params.append({"name": a.arg, "kind": "kwonly", "default": d is not None})
    if args.kwarg:
        params.append({"name": args.kwarg.arg, "kind": "varkw", "default": True})
    return params


def _decorators(node) -> set[str]:
    out = set()
    for d in node.decorator_list:
        t = d.func if isinstance(d, ast.Call) else d
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, ast.Attribute):
            out.add(t.attr)
    return out


def _public(name: str) -> bool:
    return not name.startswith("_")


def _walk_top(body):
    """Top-level statements, looking inside if/try/with blocks (version switches, optional imports)."""
    for node in body:
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            yield from _walk_top(node.body)
            for h in getattr(node, "handlers", []):
                yield from _walk_top(h.body)
            yield from _walk_top(getattr(node, "orelse", []))
            yield from _walk_top(getattr(node, "finalbody", []))
        else:
            yield node


def _resolve_relative(module: str, is_pkg: bool, level: int, target: str | None) -> str:
    parts = module.split(".")
    if not is_pkg:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    base = ".".join(parts)
    if target:
        return f"{base}.{target}" if base else target
    return base


def extract_api(mods: dict[str, str], is_pkg: dict[str, bool] | None = None) -> dict:
    """Return {"symbols": {qualname: {...}}, "warnings": [...]}."""
    symbols: dict[str, dict] = {}
    warnings: list[str] = []
    reexports: list[tuple[str, str, str]] = []     # (exported qualname, target qualname, module)
    stars: list[tuple[str, str]] = []              # (module, target module)
    alls: dict[str, set] = {}
    is_pkg = is_pkg or {m: any(o.startswith(m + ".") for o in mods) for m in mods}

    for mod, src in sorted(mods.items()):
        if not all(_public(p) for p in mod.split(".")):
            continue                                  # private modules are not public API
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            warnings.append(f"{mod}: could not parse ({e.msg} line {e.lineno})")
            continue
        symbols[mod] = {"kind": "module"}
        for node in _walk_top(tree.body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "__getattr__":
                    symbols[mod]["dynamic"] = True
                if not _public(node.name) or "overload" in _decorators(node):
                    continue
                symbols[f"{mod}.{node.name}"] = {"kind": "function", "params": _sig(node.args, False),
                                                 "line": node.lineno}
            elif isinstance(node, ast.ClassDef):
                if not _public(node.name):
                    continue
                q = f"{mod}.{node.name}"
                symbols[q] = {"kind": "class", "line": node.lineno}
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        decs = _decorators(item)
                        if "overload" in decs:
                            continue
                        if item.name == "__init__":
                            symbols[q]["params"] = _sig(item.args, True)
                            continue
                        if not _public(item.name):
                            continue
                        if decs & {"property", "cached_property"} or any(
                                d.endswith(("setter", "deleter")) for d in decs):
                            symbols[f"{q}.{item.name}"] = {"kind": "attribute"}
                        else:
                            symbols[f"{q}.{item.name}"] = {"kind": "method", "line": item.lineno,
                                                           "params": _sig(item.args, "staticmethod" not in decs)}
                    elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                        for t in (item.targets if isinstance(item, ast.Assign) else [item.target]):
                            if isinstance(t, ast.Name) and _public(t.id):
                                symbols[f"{q}.{t.id}"] = {"kind": "attribute"}
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        if t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                            alls[mod] = {e.value for e in node.value.elts
                                         if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                        elif _public(t.id):
                            symbols[f"{mod}.{t.id}"] = {"kind": "name"}
            elif isinstance(node, ast.ImportFrom):
                src_mod = (_resolve_relative(mod, is_pkg.get(mod, False), node.level, node.module)
                           if node.level else node.module or "")
                for a in node.names:
                    if a.name == "*":
                        stars.append((mod, src_mod))
                        continue
                    exported = a.asname or a.name
                    if _public(exported):
                        reexports.append((f"{mod}.{exported}", f"{src_mod}.{a.name}", mod))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname and _public(a.asname):
                        reexports.append((f"{mod}.{a.asname}", a.name, mod))

    # star imports, then re-exports (a few rounds so chains resolve)
    for _ in range(3):
        for mod, target in stars:
            names = alls.get(target)
            for q, s in list(symbols.items()):
                if q.startswith(target + ".") and "." not in q[len(target) + 1:]:
                    short = q[len(target) + 1:]
                    if (names is None or short in names) and f"{mod}.{short}" not in symbols:
                        symbols[f"{mod}.{short}"] = dict(s, via=q)
        for exported, target, _mod in reexports:
            if target in symbols and exported not in symbols:
                symbols[exported] = dict(symbols[target], via=target)
                for q, s in list(symbols.items()):          # Class.method through the re-export
                    if q.startswith(target + ".") and exported + q[len(target):] not in symbols:
                        symbols[exported + q[len(target):]] = dict(s, via=q)
    for exported, target, mod in reexports:
        # objects imported from outside the package count only when __all__ lists them
        if exported not in symbols and exported.rsplit(".", 1)[1] in alls.get(mod, ()):
            symbols[exported] = {"kind": "name", "via": target}

    # __all__ decides what `import *` brings in (handled above). A name left out of __all__ but still
    # defined keeps working with `from mod import name`, so it is not treated as removed.
    # It is marked, so a report can keep the declared API apart from module internals.
    for q, s in symbols.items():
        mod, _, short = q.rpartition(".")
        if s["kind"] != "module" and mod in alls and short not in alls[mod]:
            s["outside_all"] = True
    return {"symbols": symbols, "warnings": warnings}


# ---------------------------------------------------------------- diffing two APIs

def _positional(params):
    return [p for p in params if p["kind"] in ("posonly", "pos")]


def _has(params, kind):
    return any(p["kind"] == kind for p in params)


def compare_params(old: list[dict], new: list[dict]) -> list[str]:
    """Breaking changes between two parameter lists, as short sentences."""
    out = []
    new_by = {p["name"]: p for p in new}
    opos, npos = _positional(old), _positional(new)
    for i, p in enumerate(opos):
        if i < len(npos):
            if npos[i]["name"] != p["name"] and p["kind"] != "posonly":
                out.append(f"positional parameter {i + 1} was '{p['name']}', now '{npos[i]['name']}'")
        elif not _has(new, "var"):
            q = new_by.get(p["name"])
            if q and q["kind"] == "kwonly":
                out.append(f"'{p['name']}' became keyword-only")
            elif not q:
                out.append(f"parameter '{p['name']}' removed")
    for p in old:
        if p["kind"] in ("pos", "kwonly") and p["name"] not in new_by and not _has(new, "varkw"):
            msg = f"parameter '{p['name']}' removed"
            if msg not in out:
                out.append(msg)
        q = new_by.get(p["name"])
        if q and p["default"] and not q["default"] and q["kind"] not in ("var", "varkw"):
            out.append(f"parameter '{p['name']}' lost its default (now required)")
        if q and p["kind"] == "pos" and q["kind"] == "posonly":
            out.append(f"'{p['name']}' became positional-only")
    old_names = {p["name"] for p in old}
    for p in new:
        if not p["default"] and p["name"] not in old_names and p["kind"] not in ("var", "varkw"):
            out.append(f"new required parameter '{p['name']}'")
    if _has(old, "var") and not _has(new, "var"):
        out.append("*args no longer accepted")
    if _has(old, "varkw") and not _has(new, "varkw"):
        out.append("**kwargs no longer accepted")
    return out


def diff_api(old: dict, new: dict) -> list[dict]:
    o, n = old["symbols"], new["symbols"]
    changes = []
    for q in sorted(o):
        s = o[q]
        parent = q.rsplit(".", 1)[0]
        if q not in n:
            if parent in o and parent not in n:
                continue            # the parent's removal already covers it
            dyn = n.get(parent, {}).get("dynamic", False)
            c = {"symbol": q, "change": "removed", "kind": s["kind"], "breaking": True,
                 "details": ["removed" + (" (module has __getattr__, may still resolve)" if dyn else "")],
                 "dynamic": dyn}
            if s.get("outside_all"):
                c["outside_all"] = True
            changes.append(c)
            continue
        t = n[q]
        if s["kind"] != t["kind"] and {s["kind"], t["kind"]} != {"function", "class"}:
            changes.append({"symbol": q, "change": "kind", "kind": t["kind"], "breaking": True,
                            "details": [f"was a {s['kind']}, now a {t['kind']}"]})
            continue
        if "params" in s and "params" in t:
            d = compare_params(s["params"], t["params"])
            if d:
                changes.append({"symbol": q, "change": "signature", "kind": t["kind"], "breaking": True,
                                "details": d, "old_params": s["params"], "new_params": t["params"]})
    for q in sorted(set(n) - set(o)):
        changes.append({"symbol": q, "change": "added", "kind": n[q]["kind"], "breaking": False, "details": []})
    # a re-export (demo.Client -> demo.client.Client) with the same change is an alias, counted once
    by = {c["symbol"]: c for c in changes}
    for c in changes:
        target = (o.get(c["symbol"]) or n.get(c["symbol"]) or {}).get("via")
        t = by.get(target)
        if t and t is not c and t["change"] == c["change"] and t["details"] == c["details"]:
            c["alias_of"] = target
    return changes


# ---------------------------------------------------------------- scanning a repository

def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def check_call(call: ast.Call, old: list[dict], new: list[dict]) -> list[str]:
    """Reasons this concrete call breaks under the new signature ([] = this call is fine)."""
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        return []                                    # *args / **kwargs at the call site: cannot tell
    reasons = []
    npos, opos = _positional(new), _positional(old)
    k = len(call.args)
    kws = [kw.arg for kw in call.keywords]
    if k > len(npos) and not _has(new, "var"):
        reasons.append(f"passes {k} positional arguments, new version accepts {len(npos)}")
    accepts = {p["name"] for p in new if p["kind"] in ("pos", "kwonly")}
    for name in kws:
        if name not in accepts and not _has(new, "varkw"):
            reasons.append(f"keyword '{name}' is not accepted any more")
    given = {p["name"] for p in npos[:k]} | set(kws)
    for p in new:
        if not p["default"] and p["kind"] not in ("var", "varkw") and p["name"] not in given:
            if p["kind"] != "kwonly" and npos.index(p) < k:
                continue
            reasons.append(f"required argument '{p['name']}' is not passed")
    for i in range(min(k, len(opos), len(npos))):
        if opos[i]["name"] != npos[i]["name"]:
            reasons.append(f"positional argument {i + 1} now binds to '{npos[i]['name']}' "
                           f"(was '{opos[i]['name']}')")
    return reasons


def _iter_py(repo: Path):
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                yield Path(root) / f


def scan_repo(repo: Path, changes: list[dict], old_api: dict) -> tuple[list[dict], list[str]]:
    by_sym = {c["symbol"]: c for c in changes if c["breaking"]}
    old_syms = old_api["symbols"]
    roots = {q.split(".")[0] for q in old_syms}
    hits, warnings = [], []
    for path in _iter_py(repo):
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except (SyntaxError, ValueError) as e:
            warnings.append(f"{path.relative_to(repo).as_posix()}: could not parse ({e})")
            continue
        rel = path.relative_to(repo).as_posix()
        alias: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in roots:
                        if a.asname:
                            alias[a.asname] = a.name
                        else:
                            alias[a.name.split(".")[0]] = a.name.split(".")[0]
                        c = by_sym.get(a.name)
                        if c:
                            hits.append(_hit(rel, node, a.name, c, ["module removed"], "breaks"))
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module \
                    and node.module.split(".")[0] in roots:
                c = by_sym.get(node.module)
                if c and c["change"] == "removed":
                    hits.append(_hit(rel, node, node.module, c, ["module removed"], "breaks"))
                    continue
                for a in node.names:
                    if a.name == "*":
                        continue
                    q = f"{node.module}.{a.name}"
                    alias[a.asname or a.name] = q
                    c = by_sym.get(q)
                    if c and c["change"] == "removed":
                        cert = "review" if c.get("dynamic") else "breaks"
                        hits.append(_hit(rel, node, q, c, [f"'from {node.module} import {a.name}' fails"], cert))
        if not alias:
            continue
        seen_attr = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                q = _resolve(node.func, alias)
                c = by_sym.get(q) if q else None
                if c and c["change"] == "signature":
                    seen_attr.add(id(node.func))
                    reasons = check_call(node, c["old_params"], c["new_params"])
                    if reasons:
                        hits.append(_hit(rel, node, q, c, reasons, "breaks"))
                    elif any(isinstance(a, ast.Starred) for a in node.args) or \
                            any(k.arg is None for k in node.keywords):
                        hits.append(_hit(rel, node, q, c, ["called with *args/**kwargs; check by hand"], "review"))
            if isinstance(node, (ast.Attribute, ast.Name)) and id(node) not in seen_attr:
                if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                    continue
                q = _resolve(node, alias)
                c = by_sym.get(q) if q else None
                if not c:
                    continue
                if c["change"] in ("removed", "kind"):
                    if isinstance(node, ast.Name) and alias.get(node.id) == q:
                        continue      # already reported at the import line
                    cert = "review" if c.get("dynamic") else "breaks"
                    hits.append(_hit(rel, node, q, c, c["details"], cert))
    # one entry per (file, line, symbol)
    uniq, out = set(), []
    for h in sorted(hits, key=lambda h: (h["file"], h["line"], h["col"])):
        key = (h["file"], h["line"], h["symbol"])
        if key not in uniq:
            uniq.add(key)
            out.append(h)
    return out, warnings


def _resolve(node, alias: dict[str, str]) -> str | None:
    d = _dotted(node)
    if not d:
        return None
    head, _, rest = d.partition(".")
    if head not in alias:
        return None
    return alias[head] + ("." + rest if rest else "")


def _hit(file, node, symbol, change, reasons, certainty):
    return {"file": file, "line": node.lineno, "col": node.col_offset + 1, "symbol": symbol,
            "change": change["change"], "certainty": certainty, "reasons": reasons}


# ---------------------------------------------------------------- report

def build_report(old_spec, new_spec, repo, cache_dir=None) -> dict:
    old_mods, old_info = load_sources(old_spec, cache_dir)
    new_mods, new_info = load_sources(new_spec, cache_dir)
    if not old_mods or not new_mods:
        raise InputError("no Python modules found in " + (old_spec if not old_mods else new_spec))
    old_api, new_api = extract_api(old_mods), extract_api(new_mods)
    changes = diff_api(old_api, new_api)
    report = {
        "tool": "upgrade-radar", "version": VERSION,
        "old": old_info, "new": new_info,
        "api": {"old_symbols": len(old_api["symbols"]), "new_symbols": len(new_api["symbols"]),
                "breaking": sum(c["breaking"] and "alias_of" not in c for c in changes),
                "added": sum(c["change"] == "added" and "alias_of" not in c for c in changes)},
        "changes": [{k: v for k, v in c.items() if k not in ("old_params", "new_params")} for c in changes],
        "warnings": old_api["warnings"] + new_api["warnings"],
        "limits": [
            "static analysis only: nothing is imported or executed",
            "method calls on instances (obj.method()) are not traced to their class",
            "behaviour changes with the same signature are invisible; run your tests",
            "C extensions, dynamic attributes and runtime-generated APIs are not seen",
        ],
    }
    if repo is not None:
        hits, warns = scan_repo(Path(repo), changes, old_api)
        report["repo"] = str(repo)
        report["hits"] = hits
        report["warnings"] += warns
        report["summary"] = {"breaks": sum(h["certainty"] == "breaks" for h in hits),
                             "review": sum(h["certainty"] == "review" for h in hits)}
    return report


def exit_code(report: dict) -> int:
    if "summary" in report:
        if report["summary"]["breaks"]:
            return EXIT_BREAKS
        return EXIT_REVIEW if report["summary"]["review"] else EXIT_OK
    return EXIT_BREAKS if report["api"]["breaking"] else EXIT_OK


COLLAPSE_NAMES = 5


def format_text(report: dict, show_added: bool = False) -> str:
    a = report["api"]
    lines = [f"upgrade-radar {report['version']}: {report['old']['source']} -> {report['new']['source']}",
             f"API: {a['breaking']} breaking change(s), {a['added']} added"]
    aliases: dict[str, list] = {}
    for c in report["changes"]:
        if "alias_of" in c:
            aliases.setdefault(c["alias_of"], []).append(c["symbol"])
    # Module-level names removed in bulk (often parser constants) and names the old version left
    # out of __all__ are summarised in the text; --json keeps every one of them.
    names_by_mod: dict[str, list] = {}
    for c in report["changes"]:
        if c["breaking"] and "alias_of" not in c and c["change"] == "removed" and c["kind"] == "name" \
                and not c.get("outside_all"):
            names_by_mod.setdefault(c["symbol"].rpartition(".")[0], []).append(c)
    bulk = {id(c) for cs in names_by_mod.values() if len(cs) > COLLAPSE_NAMES for c in cs}
    outside = [c for c in report["changes"]
               if c["breaking"] and "alias_of" not in c and c.get("outside_all") and c["symbol"] not in aliases]
    for c in report["changes"]:
        if "alias_of" in c:
            continue
        if c["breaking"]:
            if id(c) in bulk or c in outside:
                continue
            also = f" [also {', '.join(aliases[c['symbol']])}]" if c["symbol"] in aliases else ""
            lines.append(f"  {c['change']:<9} {c['symbol']} ({c['kind']}){also}: " + "; ".join(c["details"]))
        elif show_added:
            lines.append(f"  added     {c['symbol']} ({c['kind']})")
    for mod, cs in sorted(names_by_mod.items()):
        if len(cs) > COLLAPSE_NAMES:
            shown = ", ".join(c["symbol"].rpartition(".")[2] for c in cs[:4])
            lines.append(f"  removed   {mod}: {len(cs)} module-level names ({shown}, ...)")
    if outside:
        shown = ", ".join(c["symbol"] for c in outside[:3])
        lines.append(f"  removed   {len(outside)} name(s) the old version left out of __all__ "
                     f"({shown}{', ...' if len(outside) > 3 else ''})")
    if bulk or outside:
        lines.append("  (every symbol is listed in --json; your own imports of them are still checked below)")
    if "hits" in report:
        s = report["summary"]
        lines.append(f"Your code ({report['repo']}): {s['breaks']} call site(s) break, {s['review']} to review")
        for h in report["hits"]:
            lines.append(f"  {h['file']}:{h['line']}:{h['col']}  {h['certainty']:<6} {h['symbol']}: "
                         + "; ".join(h["reasons"]))
        if not report["hits"]:
            lines.append("  no affected call sites found (see limits)")
    for w in report["warnings"][:10]:
        lines.append(f"warning: {w}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="upgrade-radar",
                                 description="Show which call sites in your repo break when a Python "
                                             "dependency is upgraded. Static (AST) only.")
    ap.add_argument("--version", action="version", version=f"upgrade-radar {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, hlp in (("diff", "public API differences between two versions"),
                      ("check", "API differences plus the affected call sites in --repo")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("old", help="old version: .whl, .tar.gz, .zip, directory, or pypi:NAME==VER")
        p.add_argument("new", help="new version (same forms)")
        if name == "check":
            p.add_argument("--repo", default=".", help="repository to scan (default: .)")
        p.add_argument("--json", action="store_true", help="print the full report as JSON")
        p.add_argument("--out", help="also write the JSON report to this file")
        p.add_argument("--show-added", action="store_true", help="list added symbols too")
        p.add_argument("--cache-dir", help="where pypi: downloads are kept")
    args = ap.parse_args(argv)
    try:
        report = build_report(args.old, args.new, args.repo if args.cmd == "check" else None,
                              Path(args.cache_dir) if args.cache_dir else None)
    except InputError as e:
        print(f"upgrade-radar: {e}", file=sys.stderr)
        return EXIT_BAD
    except OSError as e:
        print(f"upgrade-radar: {e}", file=sys.stderr)
        return EXIT_BAD
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text(report, args.show_added))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
