# Runtime and Windows safety

## PID liveness

- Never call `os.kill(pid, 0)` on Windows. Python exposes `CTRL_C_EVENT` as numeric zero, so the call can broadcast Ctrl+C to a console process group.
- Windows liveness checks open a limited-information process handle, query `GetExitCodeProcess`, compare with `STILL_ACTIVE`, and close the handle in `finally`.
- Invalid/non-positive PIDs are not alive. Access-denied queries are conservatively alive. No liveness check may terminate, signal, suspend, or attach a debugger to a process.

## Lock behavior

- A lock is created atomically with `O_CREAT | O_EXCL` and records PID, host, and creation time.
- A same-host live owner rejects duplicate execution. A dead or foreign stale owner may be removed and acquired once.
- Only a lock instance that successfully acquired ownership may release its lock.

## Test isolation

- After changes to locks, subprocesses, detached workers, scheduler code, or Windows control handling, run focused tests first in a hidden independent process.
- Run the full suite the same way until it completes without `KeyboardInterrupt` or host interruption.
- Disable pytest cache and Python bytecode during safety validation when a no-write audit is required.
- Never combine real browser collection, paid model calls, scheduler installation, or unbounded media work with the offline unit suite.
- Never run pytest concurrently. Parallel runs inside one window made a file deletion un-attributable to a specific test (this caused one misdiagnosis); one run, one person, serial.
- Never run `pytest .`: a positional argument overrides `testpaths` and pulls in `third_party/MediaCrawler/tests/**`, producing 18 collection errors (missing sqlalchemy/playwright). Use `pytest tests ...` or no positional argument at all.

## Pluggable material sources

- A source adapter must not raise through the pipeline: `search` returns `status="failed"` with an `error` string (a per-keyword failure is a warning), and only `MediaResolutionError` may surface per candidate.
- Signed/expiring stream URLs are memory-only: they travel via `DownloadTarget.url` and must never reach `source_report.json`, `candidate_pool.json` or any report.
- Each source supplies its own media `Referer` (`download_referer`): Douyin `https://www.douyin.com/`, bilibili `https://www.bilibili.com/`, yt-dlp none.
- bilibili needs no login: `/x/web-interface/nav` returns `data.wbi_img` even at `code=-101`, so read the keys regardless of `code`.
- bilibili: throttle between requests (default 2.0 s, `bilibili_sleep_seconds`) and back off on HTTP 412 (up to 4 attempts, `3 * attempt` s). 412 is rate control, not a missing header; it clears after a pause.

## Windows batch launchers (repo-root data loss)

- An LF-only `.bat`/`.cmd` makes `cmd.exe`'s batch reader desync line by line under `chcp 65001` + CJK text: each line loses leading bytes, so `set "RUN_LOG=..."` is swallowed and `cd /d "%PROJECT_ROOT%"` never runs (cwd stays at the caller's cwd).
- The trailing cleanup then degrades to `del /q ""`. Asymmetry that must not be forgotten: on a cmd *command line* `del /q ""` is only an rc=1 syntax error and deletes nothing; inside a *batch file* it deletes every file in the current directory. `del` never recurses, so subdirectories survive — which is why the 10 top-level repository files were wiped while every subdirectory stayed intact.
- Fix (three commits): `.gitattributes` pins `*.bat`/`*.cmd` to `text eol=crlf`; each launcher delete is `if defined RUN_LOG if exist "%RUN_LOG%" del /q "%RUN_LOG%"` (the `if defined` guard is what also catches "defined but empty").
- `.gitattributes` is not optional: this repo runs `core.autocrlf=false` and the HEAD blobs are LF-only, so rewriting only the working tree would be undone by the next checkout; only `eol=crlf` makes a fresh clone or re-checkout CRLF. Never add `* text=auto` — the tracked baseline is LF (`git ls-files --eol`), so auto-normalization would rewrite line endings repo-wide and bury real diffs.
- `tests/conftest.py` carries a session-scoped autouse guard: it snapshots the git-tracked top-level files that *exist* before the first test and re-checks them at session end, calling `pytest.fail` loudly and printing a per-file `git checkout HEAD -- "<exact path>"`. It explicitly warns never to run `git checkout -- .`. git is probed via `shutil.which("git")`, then a glob of the bundled PortableGit `*/cmd/git.exe` (version never pinned); if neither exists the guard prints `[tracked-file-guard] git unavailable - guard disabled` and must never fail silently. It is a fixture, not a `pytest_sessionstart` hook, because `tests/conftest.py` registers as a plugin only during collection — after the session-start hook has already fired.

## Incident recovery

- A Codex task interrupted after a tool call is not resumed if its session contains a call without a matching output.
- Diagnose from desktop logs and the matching session transcript. Preserve evidence, then start a fresh task using the handoff router after the underlying defect is fixed.

