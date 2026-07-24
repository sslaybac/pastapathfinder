# Running on Windows via WSL2

pastapathfinder supports Windows through **WSL2** (Windows Subsystem for Linux, version 2).
Native, non-WSL Windows is not a supported target. Inside a WSL2 distribution the tool is
installed and run exactly as on Linux — see `install.md`; there is nothing WSL-specific to
configure.

One thing about WSL2 does matter, and it is about *where your files live*, not about the
tool.

## The performance bounds require the Linux filesystem

WSL2 gives you two filesystems:

- the **Linux filesystem** — the WSL distribution's own storage, e.g. under your home
  directory `~/…` (`\\wsl$\…` from Windows); and
- the **Windows-mounted filesystem** — your Windows drives, exposed under `/mnt/c/…`,
  `/mnt/d/…`, and so on.

Access to the Windows-mounted filesystem from inside WSL2 crosses a translation layer and is
dramatically slower than the native Linux filesystem. Because analysis reads every source
file (often more than once, across incremental runs), that difference is large enough to
change whether the timing requirements hold.

Therefore (FR-31):

- **When both the target codebase and the tool's output (index and reports) reside on the
  Linux filesystem,** the performance bounds apply and hold:
  - initial analysis of a ~100,000-line codebase within **10 minutes** (FR-29), and
  - re-analysis after ≤ 5 changed files within **30 seconds** (FR-30).

  This is the recommended setup: keep the code you analyze under `~/` (or clone it there),
  and let the output directory default to its usual place under `~/.local/share` — both are
  on the Linux filesystem.

- **When the codebase resides on a Windows-mounted filesystem (`/mnt/c/…`),** the codebase is
  still fully analyzable — **every functional requirement applies** and the tool produces
  correct artifacts — but the **performance bounds (FR-29, FR-30) are not asserted.** A run
  will complete; it may simply take longer than the bounds promise on the Linux filesystem.

  Filesystem-semantics-dependent behavior — path case sensitivity and symlink handling —
  follows the semantics of the mounted Windows filesystem in that case, not Linux semantics.

## Recommendation

For a responsive experience, clone or copy the codebase you want to analyze onto the Linux
filesystem (under your WSL home directory) and run the tool there with its default output
location. Analyzing code under `/mnt/c/...` works and is correct; it is just not covered by
the speed guarantees.
