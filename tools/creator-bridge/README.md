# Creator bridge source

Runtime users do not need Go. The Windows executable is included with the application and checked against the hash in `coldstart/xhs_session.py`.

The upstream is Apache-2.0, pinned to `xpzouying/xiaohongshu-mcp` commit `3b352aa075d63cd5ebae546c9fe7d7d9b81fa7c9` (`v2.7.3`). See the included LICENSE and SOURCE.json. Modified upstream files and the additional command are retained here for review and rebuilding.

Build with Go 1.27.3 and Git: run `build.ps1` from a PowerShell session allowed to run your local scripts. It clones the pinned source into a new local build directory, verifies the commit, overlays the included files and compiles the bridge. It does not copy runtime credentials or publish anything.

Modes: `login` saves the same account's cookie and creator-origin storage; `check` opens the authenticated image form; `prepare` exercises the form with an explicit local request file and stops before the final publish action; `publish` requires that explicit request file; `audit` reads the creator's complete note management view. Only the application validates approval, quotas, gaps and immutable assets before invoking publish mode.

The bridge separately compares the creator identity with the bound main-site identity. It verifies the actual authenticated form instead of treating one cookie name as proof. It records whether the final publish operation has begun; an uncertain result after that boundary is never automatically re-sent.


Publishing opens a visible browser. If Xiaohongshu requires QR or SMS identity verification, the owner completes the requested verification in the original window. The operation waits within the configured 30-minute total deadline. After the challenge disappears, continuation is allowed at most once and only after the bound account's fully rendered small note inventory has twice refreshed without the exact original title; partial or paginated inventories require review, with account checks and a minimum wait. Unconfirmed results are retained for review. The app receives only safe progress instructions; cookies, phone numbers, verification codes and QR image data are not included in /api/state.

The six `TestColdstart` Go tests use local fixture pages and an explicitly supplied installed browser. They verify QR and SMS wait extension, unrelated-text rejection, cancellation and a single continuation. They do not verify a real social account or a real identity check. They can be run with `COLDSTART_TEST_BROWSER_BIN` set and `go test ./xiaohongshu -run TestColdstart -v`.

The `--mode install-browser` command prepares the upstream browser dependency before any cookie loading or account navigation. It reports `browser_path` and `browser_ready`, and is called by the Windows installer.

`read-comments` is a read-only account-bound mode with an explicit feed/comment request. A nonempty comment ID selects that comment and its target relationships; an empty ID returns fetched public comments for ongoing discussion monitoring. `max_comments` bounds retrieval and `has_more` reports incomplete coverage. The result retains native reply targets, which the upstream HTTP serializer omits for flat second-level threads, plus the public post's title, body, publication time and owner ID. It excludes source tokens, cookies and unrelated profile metadata. It never sends a comment.
