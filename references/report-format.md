# upgrade-radar report format (`--json` / `--out`)

```json
{
  "tool": "upgrade-radar",
  "version": "0.1.1",
  "old": {"source": "pypi:demo==1.0", "file": "demo-1.0-py3-none-any.whl", "sha256": "..."},
  "new": {"source": "demo-2.0.tar.gz", "kind": "sdist"},
  "api": {"old_symbols": 14, "new_symbols": 11, "breaking": 6, "added": 1},
  "changes": [
    {"symbol": "demo.legacy", "change": "removed", "kind": "module", "breaking": true, "details": ["removed"]},
    {"symbol": "demo.connect", "change": "signature", "kind": "function", "breaking": true,
     "details": ["positional parameter 2 was 'timeout', now 'verify'"]}
  ],
  "repo": ".",
  "hits": [
    {"file": "app.py", "line": 7, "col": 5, "symbol": "demo.connect", "change": "signature",
     "certainty": "breaks", "reasons": ["positional argument 2 now binds to 'verify' (was 'timeout')"]}
  ],
  "summary": {"breaks": 5, "review": 1},
  "warnings": ["broken.py: could not parse (...)"],
  "limits": ["static analysis only: nothing is imported or executed", "..."]
}
```

- `change`: `removed`, `signature`, `kind` (e.g. a function became a constant), `added`.
- `outside_all`: `true` on a removed symbol that the old version's module left out of its `__all__`
  (still importable by name before, so still breaking; the text output only counts these).
- `certainty`: `breaks` (the import or call fails or binds differently under the new version) or
  `review` (cannot be decided statically: `*args`/`**kwargs` at the call site, or a module with
  `__getattr__`).
- `hits` and `summary` exist only for `check`.
- `sha256` is PyPI's published digest of the downloaded file, checked before use.

Exit codes: `0` nothing breaks, `1` something breaks, `2` bad input, `3` review only.
