# AGENTS.md

## Commands

Verification order (matches CI in `.github/workflows/ci.yml`):

```bash
# 1. Compile check (all modules, incl. type-comment syntax)
python -m py_compile main.py config.py scheduler.py scanner.py sync_engine.py logger.py gui_app.py gui_diff.py gui_task_dialog.py utils/paths.py utils/timeutil.py build_exe.py make_icon.py tray.py autostart.py

# 2. Headless self-test (plain script, NOT pytest; exits 1 on failure)
python test_sync.py

# 3. Type check (mypy pinned to 1.14.1 — last version supporting Python 3.8 target)
pip install "mypy==1.14.1"
mypy --config-file mypy.ini main.py config.py scheduler.py scanner.py sync_engine.py logger.py gui_app.py gui_task_dialog.py gui_diff.py utils/paths.py utils/timeutil.py tray.py autostart.py build_exe.py make_icon.py
```

- `test_sync.py` uses a custom `check()` with one function per numbered section (`test_N()`); run the whole file (`python test_sync.py`). Section functions are also pytest-collectible, but pytest is not a dependency — the plain-script runner is canonical.
- Build Windows exe: `python build_exe.py` (needs `pyinstaller==5.13.2`, optional `pillow` for icon). Linux dist output is verification-only, not a deliverable.
- Run app: `python main.py` (GUI); headless CLI via `--list` / `--sync <name|id>` / `--autostart`. `--sync` exit codes: 0 ok, 1 task missing/disabled, 2 partial failures, 3 exception.

## Hard constraints

- **Python 3.8 compatibility is mandatory** (Windows 7 target). No 3.9+ syntax or stdlib features. CI matrix runs both 3.8 and 3.13 on Linux and Windows.
- **Type annotations use `# type:` comments**, not native annotations — the whole codebase does this and `mypy.ini` (`check_untyped_defs = False`) depends on it. Keep new code consistent.
- **Zero mandatory third-party runtime deps**: stdlib + tkinter only. Optional `xxhash` must degrade to `hashlib` when absent.
- Commit messages are Chinese with conventional prefixes (`fix:`, `feat:`).

## Architecture

- Flow: `main.py` → `scanner.py` (scan/hash/filter) → `sync_engine.py` (diff + apply + conflicts) → `config.py` (Task model, `config/tasks.json` + per-task baseline in `config/baseline/<id>.json`). `scheduler.py` is the resident background scheduler.
- **Threading contract** (`sync_engine.py` docstring): diff/apply run in a worker thread; callbacks (`on_ask`, `progress`) fire on that thread and must never touch tkinter. GUI marshals to main thread via queue.
- `tray.py` is Windows-only ctypes (zero deps); all other platforms degrade gracefully. Same pattern in `autostart.py`.
- GUI logic is tested headless via fake tkinter classes inside `test_sync.py`; the Linux dev box has no tkinter, so never try to launch the GUI there.

## Data-safety invariants (regression-tested — do not break)

- Conflict loser is backed up locally as `<name>.conflict-<ts>` before overwrite and **never propagated** to the peer side.
- Corrupt `tasks.json` is preserved as `tasks.json.corrupt-<ts>`, never overwritten; app restarts from empty config.
- Copies are atomic: write `.tmp~` then rename; scanning skips `*.tmp~` leftovers.
- Symlinks are fully ignored (no follow, no copy, no record).
- Comparison is size+mtime first; when size matches but mtime differs only within the 2s FAT32 tolerance, it falls back to content hash. The "newer wins" conflict comparison uses the same 2s tolerance.
