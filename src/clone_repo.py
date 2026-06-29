"""
clone_repo.py

PURPOSE OF THIS FILE:
This is the very first step in the pipeline. Before we can analyze any code,
we need the actual source files sitting on disk. This module takes a GitHub
URL (e.g. "https://github.com/psf/requests") and clones it into a temporary
local folder, then hands back the local path so the rest of the program can
just treat it like any normal folder of files.

WHY WE CLONE INSTEAD OF USING THE GITHUB API:
The GitHub API is great for small lookups (like "what's the latest commit"),
but our static analysis step needs to read the FULL CONTENTS of every Python
file in the repo. Doing that one file at a time over the API would mean
hundreds of network requests and we'd hit GitHub's rate limit fast on a
medium-sized repo. Cloning once and then reading from local disk is faster
and has no rate limit.
"""

import os
import stat

# shutil = "shell utilities". It's Python's built-in module for higher-level
# file/folder operations that the plain `os` module can't do in one call.
# We specifically need shutil.rmtree() later in this file: it deletes an
# entire folder AND everything inside it (subfolders, files, all of it) in
# one recursive call. Plain os.remove() only deletes a single file, and
# os.rmdir() only deletes a folder if it's already empty, neither can handle
# a cloned repo with thousands of nested files. rmtree() is the one tool that
# can tear down a whole cloned repo folder for our cleanup step below.
import shutil

import tempfile
from git import Repo  # this comes from the "gitpython" package we just installed


def clone_repo(github_url: str) -> str:
    """
    Clones a GitHub repo into a temporary folder and returns the local path.

    Args:
        github_url: something like "https://github.com/psf/requests"
                    (with or without a trailing .git, both work)

    Returns:
        The local filesystem path where the repo now lives, e.g.
        "/tmp/blastradius_abc123/requests"

    WHY tempfile.mkdtemp():
        We don't want to clone into our own project folder (that would mix
        the tool's code with the code it's analyzing). mkdtemp() creates a
        fresh, uniquely-named temp folder every time, so two different runs
        (or two different repos) never collide with each other.

    WHY WE DO A FULL CLONE (NOT depth=1 / shallow):
        We originally used depth=1 here (a "shallow" clone that only grabs
        the latest snapshot of every file, not its commit history), since
        Week 1-2 only needed current file CONTENTS to parse the code.
        Week 3 needs the actual commit HISTORY per file (how often it
        changes, how recently, how many of those commits look like bug
        fixes) to compute risk scores, and a shallow clone has none of that,
        it literally only contains 1 commit total. We measured the real
        cost of dropping shallow cloning on a medium-sized repo (Flask):
        full clone took about the same time (2-3 seconds) and similar disk
        space as the shallow version, so there's no real performance
        tradeoff here worth the complexity of doing two separate clone
        steps. If we ever hit a genuinely massive repo (huge multi-decade
        history) where this becomes slow, we can revisit with
        git fetch --unshallow as a follow-up step instead.
    """
    # Create a fresh temp directory to clone into.
    # Example result: /tmp/blastradius_xk29fa
    temp_dir = tempfile.mkdtemp(prefix="blastradius_")

    # Repo.clone_from() is GitPython's wrapper around `git clone`.
    # No depth argument here means a full clone with complete history.
    print(f"Cloning {github_url} into {temp_dir} ...")
    Repo.clone_from(github_url, temp_dir)
    print("Clone complete.")

    return temp_dir


def _remove_readonly(func, path, _):
    """
    Error handler for shutil.rmtree(), used only on Windows.

    WHY THIS EXISTS:
        Git stores some of its internal files (inside .git/objects/pack/)
        as read-only on Windows. shutil.rmtree() tries to delete them like
        any other file and Windows blocks it with a PermissionError, even
        though we genuinely have permission to remove the whole folder.
        This handler runs automatically whenever rmtree() hits that error:
        it strips the read-only flag with os.chmod(), then retries the
        exact same delete operation that failed (func is the failed
        function, e.g. os.unlink, and path is the file it failed on).
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def cleanup_repo(local_path: str) -> None:
    """
    Deletes the cloned repo from disk once we're done with it.

    WHY THIS EXISTS:
        Every clone takes up disk space. If a user analyzes 10 repos in a
        session and we never clean up, we slowly fill the disk. This function
        is the "undo" for clone_repo() above. We will call this once the
        graph has been built and saved (the graph itself is small JSON, so we
        don't need to keep the full source around after analysis).

    WHY onerror=_remove_readonly:
        Without this, cleanup crashes on Windows with a PermissionError on
        git's internal pack files (see _remove_readonly above for why).
        Passing this handler tells rmtree "if you hit a permission error
        while deleting something, try this fix-and-retry logic instead of
        just crashing." On Mac/Linux this handler simply never triggers,
        since the read-only issue is Windows-specific, so it's safe to leave
        in for everyone.
    """
    if os.path.exists(local_path):
        shutil.rmtree(local_path, onerror=_remove_readonly)
        print(f"Cleaned up {local_path}")


# --- Quick manual test ---
# This block only runs if you execute this file directly (python clone_repo.py),
# NOT when this file is imported by other parts of the program. It's a fast
# way to check the function works before we wire it into anything bigger.
if __name__ == "__main__":
    # Using a small, well-known repo for testing so the clone is fast.
    test_path = clone_repo("https://github.com/pallets/flask")
    print(f"Repo is now at: {test_path}")
    print("Top-level contents:", os.listdir(test_path))

    # Clean up after the test so we don't leave junk on disk.
    cleanup_repo(test_path)