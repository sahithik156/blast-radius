"""
build_graph.py

PURPOSE OF THIS FILE:
parse_repo.py gives us flat lists: every function we found, and every import
statement we found. That's raw material, not a usable structure. This file
turns that raw material into two real graphs we can actually query:

  1. FILE-LEVEL GRAPH: nodes = files, edges = "this file imports that file"
     This is the cleaner, coarser view. Good for the main UI: "if I touch
     tasks.py, what other files are affected?"

  2. FUNCTION-LEVEL GRAPH: nodes = individual functions, edges = "this
     function calls that function". This is the precise, noisier view.
     Good for drilling into detail: "if I change add(), which EXACT other
     functions call it?"

WHY WE NEED BOTH:
A file-level graph alone hides detail (a file might have 20 functions, only
1 of which is actually affected). A function-level graph alone is noisy and
hard to look at as a whole-repo map. So we build both, and the UI will show
file-level by default with function-level as a drill-down.

WHY THIS IS ITS OWN FILE, SEPARATE FROM parse_repo.py:
parse_repo.py's job is "read code, extract facts." This file's job is
"take those facts and build a queryable structure." Keeping them separate
means we could swap out HOW we build the graph (e.g. add JS/TS parsing
later) without touching the parsing logic at all.
"""

import os
import networkx as nx  # the graph library: handles nodes, edges, and
                        # graph-traversal queries (like "find everything
                        # reachable from this node") so we don't have to
                        # write that traversal logic ourselves


def _build_import_lookup(file_imports_list: list, repo_path: str) -> dict:
    """
    Builds a lookup table that maps "module-style import strings" to the
    actual relative file path they refer to, IF that file exists in this repo.

    WHY THIS IS NECESSARY:
    When parse_repo.py found `import celery.app`, that's just a string,
    "celery.app". We don't yet know if that refers to:
      (a) a real file in this repo, like celery/app.py, or
      (b) an external package (the actual pip-installed celery library)
    This function resolves that ambiguity by checking which actual file
    paths exist on disk, and only those count as real internal dependencies.

    HOW IT WORKS:
    For every .py file we parsed, we figure out what its "import name" would
    be if another file in the repo imported it. For example:
      celery/app.py        ->  import name "celery.app"
      celery/__init__.py    ->  import name "celery" (special case: __init__
                                 files represent the PACKAGE itself, not a
                                 submodule named "__init__")
    We build a dict: {"celery.app": "celery/app.py", "celery": "celery/__init__.py", ...}
    Then resolving an import string is just a dict lookup.
    """
    lookup = {}

    for file_imports in file_imports_list:
        filepath = file_imports.filepath  # e.g. "celery/app.py" or "celery/__init__.py"

        # Convert the file path into the dotted module name Python would use
        # to import it. Strip the .py extension first.
        module_path = filepath[:-3] if filepath.endswith(".py") else filepath
        # Replace OS-specific slashes with dots: "celery/app" -> "celery.app"
        # We use os.sep here (not a hardcoded "/") so this works correctly
        # on Windows (which uses backslashes) as well as Mac/Linux.
        module_name = module_path.replace(os.sep, ".")

        # Special case: __init__.py represents the package itself.
        # "celery/__init__.py" should resolve from the import name "celery",
        # not "celery.__init__" (nobody writes "import celery.__init__").
        if module_name.endswith(".__init__"):
            module_name = module_name[: -len(".__init__")]

        lookup[module_name] = filepath

    return lookup


def _resolve_import(module_string: str, level: int, importing_filepath: str, lookup: dict) -> str:
    """
    Given one import (a module string + its relative level) found inside
    a specific file, tries to find which actual file in the repo this
    corresponds to.

    HANDLING level (RELATIVE IMPORTS):
    level=0 means an absolute import ("from flask.ctx import x"), so we
    resolve module_string directly against the repo-root-based lookup table.

    level>=1 means a relative import ("from . import ctx" is level=1,
    "from .. import ctx" is level=2). Each level steps UP one folder from
    the importing file's own location. "from . import ctx" inside
    flask/app.py means "look for ctx in the SAME folder as app.py", i.e.
    flask/ctx.py, NOT repo-root ctx.py. So we first figure out which folder
    the relative import is anchored to, then prepend that folder onto
    module_string before doing the lookup.

    WHY WE TRY PROGRESSIVELY SHORTER PREFIXES (for the resolved string):
    Same reasoning as before, an import can reference a NAME inside a file
    rather than a file itself (e.g. "from .app import Flask" where Flask is
    a class defined inside app.py, not a file called Flask.py). So once we
    have the fully-qualified candidate string, we still try shortening it
    from the right until something in the lookup matches.

    Returns the matched relative filepath, or None if this import doesn't
    correspond to any file we found in the repo (external package).
    """
    if level > 0:
        # Step up `level` folders from the importing file's own folder.
        # Example: importing_filepath = "src/flask/app.py", level=1
        #   -> importing file's folder is "src/flask"
        #   -> level=1 means "this folder itself", so anchor = "src/flask"
        # Example: level=2 -> anchor = "src" (one folder further up)
        importing_folder = os.path.dirname(importing_filepath)
        anchor_parts = importing_folder.split(os.sep) if importing_folder else []

        # level=1 means "current folder" (step up 0 extra), level=2 means
        # step up 1 extra folder, and so on. So we remove (level - 1)
        # trailing folder segments from the anchor.
        steps_up = level - 1
        if steps_up > 0:
            anchor_parts = anchor_parts[:-steps_up] if steps_up <= len(anchor_parts) else []

        anchor = ".".join(anchor_parts)

        # Combine the anchor folder with whatever module_string was given.
        # "from . import ctx" gives module_string="ctx", anchor="src.flask"
        #   -> full candidate = "src.flask.ctx"
        # "from . import x" with NO module_string (bare "from . import x"
        # syntax has module_string="") just resolves to the anchor itself.
        if module_string:
            full_string = f"{anchor}.{module_string}" if anchor else module_string
        else:
            full_string = anchor
    else:
        # level == 0: a normal absolute import, resolve as-is.
        full_string = module_string

    if not full_string:
        return None

    parts = full_string.split(".")

    # Try the full string first, then progressively shorter prefixes, to
    # handle imports that reference a NAME inside a module rather than a
    # file/submodule itself.
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in lookup:
            return lookup[candidate]

    return None  # no match found, this is an external dependency, not internal


def build_file_graph(parsed_data: dict, repo_path: str) -> nx.DiGraph:
    """
    Builds the file-level dependency graph.

    Nodes: every .py file in the repo (even ones with no imports, so they
           still show up if something else clicks through to them).
    Edges: a directed edge from file A to file B means "A imports something
           from B". Direction matters: A depends on B, not the reverse.
           This is what makes "blast radius" queries work later: if B
           changes, we look for everything that has an edge POINTING AT B.

    WHY A DIRECTED GRAPH (DiGraph) AND NOT AN UNDIRECTED ONE:
    Dependencies are one-way. If checkout.py imports payments.py, that does
    NOT mean payments.py depends on checkout.py. An undirected graph would
    lose this distinction and give wrong answers to "what depends on this."
    """
    graph = nx.DiGraph()

    import_lookup = _build_import_lookup(parsed_data["file_imports"], repo_path)

    # First pass: add every file as a node, even before we know its edges.
    # This ensures files with zero imports (e.g. a pure utility file with no
    # dependencies) still appear in the graph instead of being silently
    # dropped, which matters for the UI later (the user should be able to
    # find ANY file, not just ones with connections).
    for file_imports in parsed_data["file_imports"]:
        graph.add_node(file_imports.filepath)

    # Second pass: resolve each import string and add an edge if it
    # corresponds to a real file in the repo.
    unresolved_count = 0
    for file_imports in parsed_data["file_imports"]:
        for module_string, level in file_imports.imports:
            resolved_path = _resolve_import(
                module_string, level, file_imports.filepath, import_lookup
            )

            if resolved_path is None:
                # This import is to an external package (e.g. "flask",
                # "os", "requests"), not something inside this repo.
                # We don't add an edge for it, since it's not part of
                # THIS repo's internal dependency structure.
                unresolved_count += 1
                continue

            if resolved_path == file_imports.filepath:
                # A file importing itself shouldn't happen in real code,
                # but if it does (e.g. a weird relative-import edge case),
                # skip it rather than adding a meaningless self-loop edge.
                continue

            graph.add_edge(file_imports.filepath, resolved_path)

    print(f"File graph: {graph.number_of_nodes()} files, "
          f"{graph.number_of_edges()} internal dependency edges "
          f"({unresolved_count} imports were external packages, skipped)")

    return graph


def build_function_graph(parsed_data: dict) -> nx.DiGraph:
    """
    Builds the function-level call graph.

    Nodes: every function we found, identified as "filepath::function_name"
           (e.g. "payments/core.py::process_payment"). WHY THIS NAMING
           SCHEME: two different files can both have a function called
           "process()". If we used just the function name as the node ID,
           those two unrelated functions would collide into a single node.
           Combining filepath + name guarantees uniqueness.

    Edges: a directed edge from function A to function B means "A calls B
           somewhere in its body", but ONLY if B is a function we actually
           found defined somewhere in this repo. Calls to built-ins
           (isinstance, len) or external library functions are dropped,
           since we have no node to point them at, and including them would
           create dangling, meaningless edges.
    """
    graph = nx.DiGraph()

    # Build a lookup from plain function name -> list of full node IDs.
    # WHY A LIST AND NOT A SINGLE VALUE: multiple functions across the repo
    # can share the same name (e.g. many classes have a method called
    # "save" or "process"). When we see a call to "save", we genuinely can't
    # always know WHICH "save" was meant without deeper type analysis (which
    # is out of scope for v1). So we connect the call to ALL same-named
    # functions we found, this may slightly overcount edges, but it's a
    # reasonable, honest approximation for a v1, and far better than silently
    # picking one at random or dropping the edge entirely.
    name_to_node_ids = {}
    for func in parsed_data["functions"]:
        node_id = f"{func.filepath}::{func.name}"
        graph.add_node(node_id, filepath=func.filepath, line_number=func.line_number)
        name_to_node_ids.setdefault(func.name, []).append(node_id)

    # Now add edges for each function's calls.
    skipped_calls = 0
    added_edges = 0
    for func in parsed_data["functions"]:
        caller_node_id = f"{func.filepath}::{func.name}"

        for called_name in func.calls:
            matching_targets = name_to_node_ids.get(called_name)

            if not matching_targets:
                # This call is to something we have no definition for
                # (a built-in like len(), or an external library function).
                skipped_calls += 1
                continue

            for target_node_id in matching_targets:
                if target_node_id == caller_node_id:
                    continue  # skip self-recursive calls for now, not useful for blast radius
                graph.add_edge(caller_node_id, target_node_id)
                added_edges += 1

    print(f"Function graph: {graph.number_of_nodes()} functions, "
          f"{added_edges} call edges "
          f"({skipped_calls} calls were to built-ins/external code, skipped)")

    return graph


# --- Quick manual test ---
if __name__ == "__main__":
    from clone_repo import clone_repo, cleanup_repo
    from parse_repo import parse_repo

    repo_path = clone_repo("https://github.com/pallets/flask")
    parsed_data = parse_repo(repo_path)

    print()
    file_graph = build_file_graph(parsed_data, repo_path)
    function_graph = build_function_graph(parsed_data)

    # Quick sanity check: pick a real file and show what it depends on,
    # and what depends on it, to confirm the graph direction is correct.
    sample_file = "src/flask/app.py"
    if sample_file in file_graph:
        print(f"\n{sample_file} imports from (depends on):")
        for dependency in file_graph.successors(sample_file):
            print(f"  -> {dependency}")

        print(f"\nFiles that depend ON {sample_file}:")
        for dependent in file_graph.predecessors(sample_file):
            print(f"  <- {dependent}")

    cleanup_repo(repo_path)