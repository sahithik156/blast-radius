"""
git_history.py

PURPOSE OF THIS FILE:
build_graph.py tells us STRUCTURE (what depends on what). This file adds
the second ingredient for risk scoring: HISTORY (how often a file changes,
how recently, and how many of those changes look like bug fixes). A file
that's central in the dependency graph AND has been repeatedly bug-fixed
recently is a much stronger risk signal than either fact alone, this file
is what lets us compute that second half.

WHAT WE'RE EXTRACTING, PER FILE:
  1. commit_count: how many commits have touched this file, ever
  2. days_since_last_commit: how recently was it last changed
  3. bugfix_commit_count: how many of those commits LOOK LIKE bug fixes,
     based on keywords in the commit message

WHY THE BUG-FIX DETECTION IS A HEURISTIC, NOT A FACT:
There's no reliable, universal way to know if a commit was "a bug fix" just
from git data alone, commit messages are written by humans and vary a lot.
We approximate it by checking for common keywords (fix, bug, patch, issue,
error). This WILL miss some real bug fixes (e.g. "resolve session leak"
doesn't contain any of our keywords) and WILL occasionally flag something
that isn't really a bug (e.g. "fix typo in README"). This is a known,
honest limitation, not a hidden one, we should surface it in the UI later
as "commits that mention fixes" rather than claiming certainty.
"""

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from git import Repo

# Keywords we look for in commit messages to approximate "this was a bug fix".
# Using a set for fast lookup, and keeping this list short and high-confidence
# rather than trying to catch every possible phrasing, a shorter list with
# fewer false positives is more trustworthy than a long list that flags
# nearly everything as a "bug fix".
BUGFIX_KEYWORDS = {"fix", "fixed", "fixes", "bug", "bugfix", "patch", "issue", "error"}


@dataclass
class FileHistory:
    """
    Git-derived stats for a single file, used as risk-scoring input later.
    """
    filepath: str
    commit_count: int
    days_since_last_commit: int
    bugfix_commit_count: int


def _looks_like_bugfix(commit_message: str) -> bool:
    """
    Checks whether a commit message contains any of our bug-fix keywords.

    WHY WE ONLY CHECK THE FIRST LINE (THE SUMMARY), NOT THE FULL MESSAGE BODY:
    We originally checked the entire commit message, but testing this against
    real history (Flask) showed it produces false positives: a commit titled
    "refactor lazy loading" with a body that happens to mention "exceptions"
    and "error" while explaining the refactor got flagged as a bug fix, even
    though it isn't one. The first line of a commit message is where authors
    conventionally state the PURPOSE of the commit ("fix X", "add Y", "
    refactor Z"), the body is often just supporting explanation that can
    mention unrelated words in passing. Checking only the summary line
    measurably reduces false positives, though it doesn't eliminate them
    entirely, this remains a heuristic, not a guarantee.

    WHY WE USE A REGEX WORD-BOUNDARY CHECK INSTEAD OF A PLAIN SUBSTRING CHECK:
    A naive `"fix" in message.lower()` would also match unrelated words like
    "prefix" or "suffix tree", since "fix" is a substring of those too. Using
    \\b (word boundary) in the regex ensures we only match "fix" as a whole
    word, not as part of a longer unrelated word.
    """
    summary_line = commit_message.strip().split("\n")[0]
    summary_lower = summary_line.lower()
    for keyword in BUGFIX_KEYWORDS:
        if re.search(rf"\b{keyword}\b", summary_lower):
            return True
    return False


def get_file_history(repo: Repo, relative_filepath: str) -> FileHistory:
    """
    Computes git history stats for ONE file.

    Args:
        repo: an ALREADY-OPEN GitPython Repo object (see get_all_file_histories
              below for why we now pass this in rather than creating it here)
        relative_filepath: path to the file RELATIVE to the repo root
                            (e.g. "src/flask/app.py"), matching the format
                            used throughout build_graph.py and parse_repo.py

    WHY THIS USED TO OPEN ITS OWN Repo OBJECT, AND WHY WE CHANGED IT:
    The original version called Repo(repo_path) fresh inside this function,
    once per file. On a repo with 83 files, that meant opening 83 separate
    Repo objects. This caused a real bug on Windows: GitPython's Repo
    objects hold open file handles into the .git folder (specifically its
    internal pack files), and Python's garbage collector doesn't always
    release those handles immediately, especially after many repeated opens
    in a tight loop. By the time cleanup_repo() tried to delete the whole
    folder, one of those handles was often still open, causing
    "PermissionError: The process cannot access the file because it is
    being used by another process." Sharing ONE Repo object across all
    files (opened once in get_all_file_histories, passed in here) avoids
    creating dozens of redundant handles, and explicitly closing it after
    we're done (see get_all_file_histories) ensures Windows releases the
    lock before we try to delete the folder.
    """
    # iter_commits with paths= filters git log to ONLY commits that touched
    # this specific file, exactly like running `git log -- path/to/file.py`
    # on the command line. This is real git history, not anything we're
    # inferring or guessing.
    commits = list(repo.iter_commits(paths=relative_filepath))

    if not commits:
        # A file with zero commit history is unusual (every tracked file
        # should have at least the commit that added it), but we handle it
        # defensively rather than crashing, e.g. this can happen for files
        # that were just added in an uncommitted working-tree change.
        return FileHistory(
            filepath=relative_filepath,
            commit_count=0,
            days_since_last_commit=-1,  # -1 signals "unknown", not "0 days ago"
            bugfix_commit_count=0,
        )

    commit_count = len(commits)

    # commits[0] is the MOST RECENT commit (iter_commits returns newest
    # first, matching `git log`'s default order).
    most_recent_commit_date = commits[0].committed_datetime
    now = datetime.now(timezone.utc)
    days_since_last_commit = (now - most_recent_commit_date).days

    bugfix_commit_count = sum(
        1 for commit in commits if _looks_like_bugfix(commit.message)
    )

    return FileHistory(
        filepath=relative_filepath,
        commit_count=commit_count,
        days_since_last_commit=days_since_last_commit,
        bugfix_commit_count=bugfix_commit_count,
    )


def get_all_file_histories(repo_path: str, file_paths: list) -> dict:
    """
    Computes git history stats for EVERY file in the repo, given a list of
    relative file paths (typically the same list build_graph.py already
    has as graph nodes).

    Returns:
        A dict mapping filepath -> FileHistory, so other modules can look
        up "what's the history for this specific file" in one step,
        the same lookup-table pattern we used for import resolution in
        build_graph.py.

    WHY WE TAKE file_paths AS AN ARGUMENT INSTEAD OF DISCOVERING FILES
    OURSELVES:
    parse_repo.py already walked the repo and found every Python file once.
    Re-walking the filesystem here would be duplicate work, and worse, could
    drift out of sync if the two file-discovery implementations ever
    disagreed. Reusing the exact same file list keeps history data and
    graph data aligned to the same set of files.

    WHY WE OPEN ONE Repo OBJECT HERE AND CLOSE IT EXPLICITLY:
    See the docstring on get_file_history() above for the full explanation,
    short version: opening dozens of separate Repo objects (one per file)
    left Windows file handles open into the .git folder, which caused
    cleanup_repo() to fail with a PermissionError afterward. Opening ONE
    Repo object here, reusing it for every file, and calling repo.close()
    once we're done ensures the lock is released before the caller tries
    to delete the cloned repo folder.
    """
    print(f"Computing git history for {len(file_paths)} files...")

    repo = Repo(repo_path)
    try:
        histories = {}
        for filepath in file_paths:
            histories[filepath] = get_file_history(repo, filepath)
    finally:
        # WHY finally: even if something goes wrong partway through (e.g.
        # one file's history computation raises an error), we still want
        # to release the git handle rather than leaving it open. A crash
        # shouldn't ALSO leave the folder permanently undeletable.
        repo.close()

    print("Git history computation complete.")
    return histories


# --- Quick manual test ---
if __name__ == "__main__":
    from clone_repo import clone_repo, cleanup_repo

    repo_path = clone_repo("https://github.com/pallets/flask")

    # Test on one well-known, actively-changed file.
    repo = Repo(repo_path)
    history = get_file_history(repo, os.path.join("src", "flask", "app.py"))
    repo.close()  # release the handle before cleanup, same reasoning as above

    print(f"\n{history.filepath}:")
    print(f"  commits: {history.commit_count}")
    print(f"  days since last commit: {history.days_since_last_commit}")
    print(f"  bugfix-looking commits: {history.bugfix_commit_count}")

    cleanup_repo(repo_path)