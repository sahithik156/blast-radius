"""
api.py

PURPOSE OF THIS FILE:
This is where everything we've built (clone_repo, parse_repo, build_graph,
git_history, risk_score) gets wired together into ONE thing the outside
world can actually call: a web API endpoint. Right now, using this tool
means running a Python script by hand and reading printed output. After
this file, you can send a GitHub URL to a URL on your own machine and get
back structured JSON, which is exactly what a web UI (Week 6) will need to
render the graph and risk scores visually.

WHY FastAPI:
FastAPI is a Python web framework built specifically for APIs (as opposed to
Flask/Django, which are more general-purpose web frameworks meant for full
websites with HTML pages). It automatically validates incoming request data
against the types we declare, and automatically generates interactive API
documentation (visiting /docs in a browser), which is useful for testing
this without building the frontend first.

WHY THIS IS A SINGLE SYNCHRONOUS ENDPOINT (NOT A BACKGROUND JOB):
A real production tool analyzing huge repos might want to kick off analysis
as a background job and let the user poll for progress, since the full
pipeline (clone + parse + graph + history) can take anywhere from a few
seconds to over a minute depending on repo size. For v1, we're keeping this
simple: the endpoint just runs the whole pipeline and returns the complete
result when it's done. This means a slow request for big repos, but it's
far simpler to build, test, and reason about, and is the right tradeoff
until we've confirmed the basic flow actually works end-to-end.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from clone_repo import clone_repo, cleanup_repo
from parse_repo import parse_repo
from build_graph import build_file_graph
from git_history import get_all_file_histories
from risk_score import compute_risk_scores

app = FastAPI(title="Blast Radius API")

# WHY CORS MIDDLEWARE IS NEEDED:
# The future web UI (Week 6) will be a webpage running in the browser,
# possibly served from a different port/origin than this API (e.g. the UI
# on localhost:3000, this API on localhost:8000). Browsers block requests
# between different origins by default for security (this is called CORS,
# Cross-Origin Resource Sharing). Without this middleware, the browser
# would silently refuse to let the UI talk to this API at all. allow_origins
# set to "*" means "any origin can call this API", which is fine for local
# development; if this were ever deployed publicly, we'd want to restrict
# this to the UI's actual specific domain instead.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    """
    The shape of the JSON the frontend sends us when asking for an analysis.

    WHY A pydantic BaseModel INSTEAD OF JUST READING RAW JSON:
    FastAPI uses this class definition to automatically validate incoming
    requests, if someone sends a request missing "github_url" or sends it
    as the wrong type, FastAPI rejects it with a clear error BEFORE our
    code even runs, so we don't need to manually check
    `if "github_url" not in request_data` everywhere.
    """
    github_url: str


@app.get("/")
def root():
    """
    A trivial health-check endpoint. Visiting this confirms the server is
    actually running and reachable, useful as a first, fast sanity check
    before testing the real (much slower) /analyze endpoint.
    """
    return {"status": "ok", "service": "blast-radius-api"}


@app.post("/analyze")
def analyze_repo(request: AnalyzeRequest):
    """
    The main endpoint. Takes a GitHub URL, runs the full pipeline, and
    returns the file graph (as a list of edges), risk scores for every
    file, and some summary stats.

    WHY WE WRAP THE WHOLE PIPELINE IN try/finally:
    Every step here (clone, parse, build graph, get history) can fail for
    reasons outside our control, a malformed URL, a private repo we can't
    access, a repo with no Python files, etc. If any step raises an
    exception, we still want to clean up the cloned repo folder before the
    error propagates, otherwise a failed analysis would leave a temp folder
    behind forever, on every single failure. The try/finally guarantees
    cleanup runs whether the pipeline succeeds OR crashes partway through.
    """
    repo_path = None
    try:
        # Step 1: clone
        repo_path = clone_repo(request.github_url)

        # Step 2: parse every Python file into functions + imports
        parsed_data = parse_repo(repo_path)

        if parsed_data["files_parsed"] == 0:
            # WHY WE CHECK THIS EXPLICITLY: a repo with zero Python files
            # (e.g. someone pastes a JS-only repo, since we only support
            # Python for v1) would otherwise silently produce an empty,
            # confusing result. Better to fail clearly and immediately than
            # return a technically-valid but useless empty response.
            raise HTTPException(
                status_code=400,
                detail="No Python files found in this repository. "
                       "This tool currently only supports Python repos.",
            )

        # Step 3: build the file-level dependency graph
        file_graph = build_file_graph(parsed_data, repo_path)

        # Step 4: pull git history for every file
        filepaths = [fi.filepath for fi in parsed_data["file_imports"]]
        histories = get_all_file_histories(repo_path, filepaths)

        # Step 5: combine graph + history into risk scores
        risk_scores = compute_risk_scores(file_graph, histories)

        # --- Build the JSON response ---
        # WHY WE CONVERT THE GRAPH TO A LIST OF EDGES INSTEAD OF RETURNING
        # THE networkx OBJECT DIRECTLY: networkx graph objects aren't JSON-
        # serializable, JSON only understands basic types (strings, numbers,
        # lists, dicts/objects). We convert the graph into the simplest
        # possible JSON-friendly shape: a flat list of {"from": ..., "to":
        # ...} edges. This is exactly the shape most graph-drawing UI
        # libraries (which we'll use in Week 6) expect as input.
        edges = [{"from": source, "to": target} for source, target in file_graph.edges()]

        # Sort files by risk score descending, so the frontend can show the
        # highest-risk files first without needing to re-sort itself.
        sorted_scores = sorted(
            risk_scores.values(), key=lambda r: r.score, reverse=True
        )

        files_response = [
            {
                "filepath": r.filepath,
                "risk_score": r.score,
                "breakdown": r.breakdown,
            }
            for r in sorted_scores
        ]

        return {
            "repo_url": request.github_url,
            "summary": {
                "total_files": parsed_data["files_parsed"],
                "total_functions": len(parsed_data["functions"]),
                "total_dependency_edges": file_graph.number_of_edges(),
            },
            "files": files_response,
            "edges": edges,
        }

    except HTTPException:
        # Re-raise HTTPExceptions as-is (like the "no Python files" case
        # above), we don't want to swallow these into a generic 500 error,
        # since they already carry a specific, useful status code and
        # message for the frontend to display.
        raise

    except Exception as e:
        # Any OTHER unexpected error (a bad URL, a network failure during
        # clone, etc.) gets converted into a clean HTTP error rather than
        # crashing the whole server process or leaking a raw Python
        # traceback to whoever called the API.
        error_text = str(e)

        # WHY WE SPECIAL-CASE THIS: a mistyped or private/nonexistent repo
        # URL is the single most likely real-world mistake a user will hit
        # with this tool. GitPython's raw error for this case is a wall of
        # git command-line internals ("Cmd('git') failed due to: exit
        # code(128)... fatal: could not read Username"), which is accurate
        # but unhelpful to read. We detect this specific, common failure
        # signature and replace it with a one-line, actionable message
        # instead. Any OTHER kind of failure still falls through to the
        # generic message below, we're not trying to catch every possible
        # error, just the one we know users will hit constantly.
        if "could not read Username" in error_text or "Repository not found" in error_text:
            raise HTTPException(
                status_code=400,
                detail="Could not access that repository. Check that the URL is "
                       "correct and the repo is public (private repos aren't "
                       "supported yet).",
            )

        raise HTTPException(status_code=500, detail=f"Analysis failed: {error_text}")

    finally:
        # Always clean up the cloned repo, whether we succeeded or hit an
        # error above, see the function docstring for the full reasoning.
        if repo_path:
            cleanup_repo(repo_path)