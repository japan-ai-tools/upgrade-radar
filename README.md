# upgrade-radar

**Which lines of your code break when you bump a Python dependency.**

```console
$ python3 scripts/upgrade_radar.py check demo-1.0-py3-none-any.whl demo-2.0.tar.gz --repo .
  removed   demo.legacy (module): removed
  signature demo.client.connect (function) [also demo.connect]: positional parameter 2 was 'timeout', now 'verify'; positional parameter 3 was 'verify', now 'timeout'
  signature demo.client.Client (class) [also demo.Client]: 'timeout' became keyword-only; new required parameter 'token'
  app.py:5:1   breaks demo.legacy: module removed
  app.py:7:5   breaks demo.connect: positional argument 2 now binds to 'verify' (was 'timeout')
  app.py:9:5   breaks demo.util.slugify: keyword 'sep' is not accepted any more
  app.py:11:10 breaks demo.Client: passes 2 positional arguments, new version accepts 1; required argument 'token' is not passed
  app.py:13:5  review demo.connect: called with *args/**kwargs; check by hand
```

(Lines from the real output for the test fixture in `tests/`, shortened. A test checks that every
line above appears in the actual output.) `ok = connect("https://x", timeout=5)` on line 8 of the
same file is not reported: it still binds correctly.

- Reads both versions with Python's `ast` module. Nothing from the package is imported or run.
- Reports only the calls that actually break: `connect(url, timeout=5)` is not flagged when
  `timeout` merely moved; `connect(url, 5)` is, because `5` now lands in another parameter.
- Python 3.9+ standard library only. No dependencies. Network only for the `pypi:` form.
- Works as an Agent Skill (`SKILL.md`) for Claude Code, Codex, Gemini CLI and Cursor, or as a
  plain CLI.

## Install

As a skill (Claude Code):

```sh
git clone https://github.com/japan-ai-tools/upgrade-radar ~/.claude/skills/upgrade-radar
```

Other agents: clone anywhere and point the agent at `SKILL.md`, or just run the script.

## Use

```sh
# API differences only
python3 scripts/upgrade_radar.py diff pypi:NAME==OLD pypi:NAME==NEW

# differences plus your affected call sites
python3 scripts/upgrade_radar.py check pypi:NAME==OLD pypi:NAME==NEW --repo .

# offline: local wheels, sdists or source directories
python3 scripts/upgrade_radar.py check old.whl new.tar.gz --repo . --json --out report.json
```

Try it on a real upgrade: `examples/demo-app/app.py` is written against `packaging` 21.x
(`packaging` 22.0 removed `LegacyVersion`):

```sh
python3 scripts/upgrade_radar.py check pypi:packaging==21.3 pypi:packaging==22.0 --repo examples/demo-app
```

Result of that run (2026-11-25, upgrade-radar 0.1.1): 54 breaking API changes in `packaging`, of
which 48 are internal names (29 pyparsing grammar constants in `packaging.requirements`, 19 names
the old version left out of `__all__`). The text output folds those into two summary lines and
lists the 6 others; `--json` keeps all 54. Your code: 2 call sites break (`app.py` lines 2 and 5,
both `LegacyVersion`).

Exit codes: `0` nothing breaks, `1` something breaks, `3` review only, `2` bad input.
JSON fields: [`references/report-format.md`](references/report-format.md).

## What it does not do

- It does not see behaviour changes behind an unchanged signature. Run your tests.
- It does not follow method calls on instances (`client.get(...)`) back to their class.
  Constructors and class-level access are followed.
- It does not handle C extensions or APIs generated at runtime.
- A clean result is not a guarantee. Treat it as a list of places to fix first.

## Tested

`python -m unittest discover -s tests` runs 17 tests (wheel, sdist and directory inputs,
`__all__`, star re-exports, path-traversal entries in archives, call binding rules, CLI exit
codes, and that the example above is real output). No network in the tests. Not yet checked
against large real-world packages; reports of wrong results are welcome as issues.

## License

MIT

---

## 日本語

Python のライブラリを上げると、**自分のコードのどの行が壊れるか**を、ファイル名と行番号つきで出します。

- 2つの版(wheel・sdist・フォルダ・`pypi:名前==版`)の公開 API を `ast` で読み比べます。ライブラリのコードは読み込まず、実行もしません
- 消えたモジュール・関数・クラス、名前が変わった引数・消えた引数、新しく必須になった引数、位置の入れ替わりを見つけます
- 「本当に壊れる呼び出し」だけを出します。キーワードで渡している引数の位置が変わっただけなら出しません
- 標準ライブラリだけ。ネットにつなぐのは `pypi:` の形を使った時だけです(PyPI が公開している sha256 で確かめます)
- できないこと: 引数が同じで中身の動きが変わった所は見えません。インスタンスのメソッド呼び出し(`obj.method()`)はクラスまでたどりません。結果が0件でも安全の保証ではないので、テストは必ず回してください
