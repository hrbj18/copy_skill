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

## Pluggable material sources

- A source adapter must not raise through the pipeline: `search` returns `status="failed"` with an `error` string (a per-keyword failure is a warning), and only `MediaResolutionError` may surface per candidate.
- Signed/expiring stream URLs are memory-only: they travel via `DownloadTarget.url` and must never reach `source_report.json`, `candidate_pool.json` or any report.
- Each source supplies its own media `Referer` (`download_referer`): Douyin `https://www.douyin.com/`, bilibili `https://www.bilibili.com/`, yt-dlp none.
- bilibili needs no login: `/x/web-interface/nav` returns `data.wbi_img` even at `code=-101`, so read the keys regardless of `code`.
- bilibili: throttle between requests (default 2.0 s, `bilibili_sleep_seconds`) and back off on HTTP 412 (up to 4 attempts, `3 * attempt` s). 412 is rate control, not a missing header; it clears after a pause.

## Incident recovery

- A Codex task interrupted after a tool call is not resumed if its session contains a call without a matching output.
- Diagnose from desktop logs and the matching session transcript. Preserve evidence, then start a fresh task using the handoff router after the underlying defect is fixed.

