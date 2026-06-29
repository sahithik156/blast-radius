"""
risk_score.py

PURPOSE OF THIS FILE:
This is where the tool's two separate data sources finally combine into the
single number the user actually cares about: "how risky is changing this
file?" build_graph.py gave us STRUCTURE (how many things depend on this
file). git_history.py gave us HISTORY (how often it changes, how recently,
how many bug-fix-looking commits). This file combines both into one 0-100
score, plus a breakdown of WHY, so the score is explainable, not a black box.

WHY WE NORMALIZE EACH SIGNAL BEFORE COMBINING:
Fan-in, commit count, and bugfix count are three different units (a count of
files, a count of commits, a count of commits again but a different
subset). We can't just add raw numbers together, "144 commits" would
completely dominate "11 dependents" purely because it's a bigger number,
not because commit count actually matters 13x more. So we convert each
signal to a comparable 0-1 scale first, THEN combine them with explicit
weights we control.

WHY WE CAP AT A PERCENTILE INSTEAD OF THE ABSOLUTE MAXIMUM:
We checked real distributions on Flask: fan-in ranges from 0 to 22, but the
MEDIAN is 0, most files have very low fan-in and a small number of central
files have much higher fan-in. If we normalized using the single highest
value in the repo as "100%", one extreme outlier file would squash every
other file's score down near zero, making the scores useless for telling
"somewhat risky" apart from "totally safe". Capping at the 90th percentile
(and treating anything above that as simply "maxed out at 100%") keeps the
scale meaningful for the bulk of normal files, at the cost of not
further distinguishing between the few most extreme files, which is an
acceptable tradeoff since those files are already clearly flagged as
highest-risk either way.
"""

from dataclasses import dataclass, field

import networkx as nx

# How much each signal contributes to the final score. These three must sum
# to 1.0, since each individual signal is normalized to a 0-1 scale, and we
# want the combined result to also land in 0-1 before scaling up to 0-100.
# WHY THESE SPECIFIC WEIGHTS: fan-in (blast radius) and bug-fix history are
# weighted equally and highest, since both are strong, direct signals of
# real risk. Recency is weighted lower, since "touched recently" is a much
# weaker/noisier signal on its own (lots of totally safe maintenance commits
# happen recently too), it should nudge the score, not dominate it.
WEIGHT_FANIN = 0.4
WEIGHT_BUGFIX = 0.4
WEIGHT_RECENCY = 0.2

# What percentile to use as the "100%" ceiling when normalizing fan-in and
# bugfix counts, see the file-level docstring above for why we use a
# percentile instead of the raw maximum.
NORMALIZATION_PERCENTILE = 90


@dataclass
class RiskScore:
    """
    The final, explainable risk assessment for one file.

    WHY breakdown IS A SEPARATE FIELD FROM score:
    The numeric score alone ("73/100") doesn't tell the user WHY a file is
    risky. The breakdown field holds the underlying facts (the actual fan-in
    count, the actual bugfix count, etc.) so the UI (and later, the LLM
    narrator) can explain the score using real numbers, not vague language.
    """
    filepath: str
    score: int  # 0-100, rounded for display
    breakdown: dict = field(default_factory=dict)


def _percentile(values: list, percentile: int) -> float:
    """
    Computes the Nth percentile of a list of numbers using simple linear
    interpolation (this is the same basic method spreadsheet software uses).

    WHY WE WRITE THIS OURSELVES INSTEAD OF USING A LIBRARY:
    Python's standard library doesn't include percentile calculation built
    in (statistics.quantiles exists but has slightly different rounding
    behavior across Python versions), and pulling in numpy as a dependency
    for one small calculation felt heavier than necessary for something
    this simple. This implementation is short, easy to verify by hand on
    a small example, and has no extra dependency.
    """
    if not values:
        return 0.0

    sorted_values = sorted(values)
    n = len(sorted_values)

    if n == 1:
        return sorted_values[0]

    # Position in the sorted list this percentile falls at, e.g. the 90th
    # percentile of a 10-item list falls at index 0.9 * (10-1) = 8.1,
    # which is 90% of the way between index 8 and index 9.
    position = (percentile / 100) * (n - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, n - 1)
    fraction = position - lower_index

    return sorted_values[lower_index] + fraction * (
        sorted_values[upper_index] - sorted_values[lower_index]
    )


def _normalize(value: float, cap: float) -> float:
    """
    Converts a raw count into a 0-1 scale, where `cap` represents "100%".
    Anything at or above `cap` is treated as a full 1.0 (maxed out), rather
    than letting values above the cap produce a normalized score greater
    than 1, which would break the weighted combination later.
    """
    if cap <= 0:
        # Defensive case: if every file in the repo has 0 of this signal
        # (e.g. a brand new repo with no bugfix-looking commits anywhere),
        # there's nothing to normalize against, treat as 0 risk contribution
        # from this signal rather than dividing by zero.
        return 0.0
    return min(value / cap, 1.0)


def compute_risk_scores(file_graph: nx.DiGraph, file_histories: dict) -> dict:
    """
    Computes a RiskScore for every file in the graph.

    Args:
        file_graph: the file-level dependency graph from build_graph.py
        file_histories: dict of filepath -> FileHistory from git_history.py

    Returns:
        dict of filepath -> RiskScore
    """
    all_filepaths = list(file_graph.nodes())

    # --- Step 1: compute the raw fan-in for every file ---
    # Fan-in here means TOTAL dependents, not just direct ones: everything
    # that could be affected if this file changes, directly or through a
    # chain of other files. nx.ancestors() gives us exactly this: every node
    # that has a path LEADING TO the given node in the directed graph.
    fanins = {}
    for filepath in all_filepaths:
        fanins[filepath] = len(nx.ancestors(file_graph, filepath))

    # --- Step 2: figure out the normalization caps from real data ---
    # We use the file_histories dict's own values for commit/bugfix data
    # (already computed), and the fanins dict we just built.
    fanin_values = list(fanins.values())
    bugfix_values = [h.bugfix_commit_count for h in file_histories.values()]

    fanin_cap = _percentile(fanin_values, NORMALIZATION_PERCENTILE)
    bugfix_cap = _percentile(bugfix_values, NORMALIZATION_PERCENTILE)

    # WHY RECENCY GETS A DIFFERENT TREATMENT (FIXED CAP, NOT PERCENTILE-BASED):
    # Fan-in and bugfix count are "more is riskier", so percentile-based
    # capping makes sense for both. Recency is different, it's "more RECENT
    # is riskier" (fewer days = more risk), and the natural ceiling is a
    # fixed, meaningful real-world threshold rather than something derived
    # from this specific repo's distribution. We treat anything touched
    # within the last 30 days as "fully recent" (1.0) and anything 365+
    # days old as "fully stale" (0.0), with linear interpolation between,
    # since "changed a month ago" vs "changed 3 years ago" is a meaningful,
    # stable real-world distinction regardless of which repo we're looking at.
    RECENCY_FULL_RISK_DAYS = 30
    RECENCY_ZERO_RISK_DAYS = 365

    results = {}
    for filepath in all_filepaths:
        history = file_histories.get(filepath)
        fanin = fanins[filepath]

        fanin_normalized = _normalize(fanin, fanin_cap)

        if history is None or history.days_since_last_commit < 0:
            # No history data available for this file, treat both
            # bugfix and recency signals as zero contribution rather than
            # guessing, an absence of data shouldn't be silently treated
            # as either "very risky" or "very safe".
            bugfix_normalized = 0.0
            recency_normalized = 0.0
            bugfix_count = 0
            commit_count = 0
            days_since_last_commit = -1
        else:
            bugfix_normalized = _normalize(history.bugfix_commit_count, bugfix_cap)
            bugfix_count = history.bugfix_commit_count
            commit_count = history.commit_count
            days_since_last_commit = history.days_since_last_commit

            if days_since_last_commit <= RECENCY_FULL_RISK_DAYS:
                recency_normalized = 1.0
            elif days_since_last_commit >= RECENCY_ZERO_RISK_DAYS:
                recency_normalized = 0.0
            else:
                # Linear interpolation between the two thresholds.
                span = RECENCY_ZERO_RISK_DAYS - RECENCY_FULL_RISK_DAYS
                recency_normalized = 1.0 - (
                    (days_since_last_commit - RECENCY_FULL_RISK_DAYS) / span
                )

        combined = (
            WEIGHT_FANIN * fanin_normalized
            + WEIGHT_BUGFIX * bugfix_normalized
            + WEIGHT_RECENCY * recency_normalized
        )
        score = round(combined * 100)

        results[filepath] = RiskScore(
            filepath=filepath,
            score=score,
            breakdown={
                "fanin_count": fanin,
                "fanin_normalized": round(fanin_normalized, 2),
                "bugfix_count": bugfix_count,
                "bugfix_normalized": round(bugfix_normalized, 2),
                "commit_count": commit_count,
                "days_since_last_commit": days_since_last_commit,
                "recency_normalized": round(recency_normalized, 2),
            },
        )

    return results


# --- Quick manual test ---
if __name__ == "__main__":
    from clone_repo import clone_repo, cleanup_repo
    from parse_repo import parse_repo
    from build_graph import build_file_graph
    from git_history import get_all_file_histories

    repo_path = clone_repo("https://github.com/pallets/flask")
    parsed_data = parse_repo(repo_path)
    file_graph = build_file_graph(parsed_data, repo_path)

    filepaths = [fi.filepath for fi in parsed_data["file_imports"]]
    histories = get_all_file_histories(repo_path, filepaths)

    risk_scores = compute_risk_scores(file_graph, histories)

    # Show the 5 highest-risk files, this is the actual "useful output" of
    # the whole tool: a ranked list of what to be most careful touching.
    top_5 = sorted(risk_scores.values(), key=lambda r: r.score, reverse=True)[:5]

    print("\nTop 5 highest-risk files:")
    for r in top_5:
        b = r.breakdown
        print(f"\n  {r.filepath}  [risk: {r.score}/100]")
        print(f"    fan-in: {b['fanin_count']} files depend on this (directly or indirectly)")
        print(f"    bugfix-looking commits: {b['bugfix_count']} (out of {b['commit_count']} total commits)")
        print(f"    last changed: {b['days_since_last_commit']} days ago")

    cleanup_repo(repo_path)