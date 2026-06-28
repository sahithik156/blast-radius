"""
parse_repo.py

PURPOSE OF THIS FILE:
This is where the "real static analysis" happens, the part that makes this
tool different from an LLM just guessing at code structure. We use Python's
built-in `ast` module (Abstract Syntax Tree) to actually parse each file the
same way the Python interpreter would, so what we extract is GROUND TRUTH,
not a guess.

WHAT WE'RE EXTRACTING:
For every .py file in the repo, we want two things:
  1. Every function/method DEFINED in that file (so we know what nodes exist)
  2. Every function CALL and import made in that file (so we know the edges,
     i.e. "this file/function depends on that one")

WHY ast AND NOT JUST REGEX OR STRING SEARCHING:
Regex-matching for "def foo(" or "import bar" looks tempting but breaks on
real code constantly: multi-line imports, decorated functions, functions
defined inside classes vs at module level, commented-out code that still
matches the pattern, etc. The `ast` module parses the file into a tree
structure that correctly understands all of this, because it's the same
parser logic Python itself uses to run the code.
"""

import ast
import os
from dataclasses import dataclass, field


@dataclass
class FunctionDef:
    """
    Represents one function or method we found defined somewhere in the repo.

    WHY A DATACLASS:
    We could just use a dict here, but a dataclass gives us named fields with
    auto-completion and catches typos (e.g. accidentally writing `.file_path`
    instead of `.filepath` would be a silent bug with a dict, but an error
    with a dataclass... well, not quite, but it documents the shape clearly).
    """
    name: str           # e.g. "process_payment"
    filepath: str        # e.g. "payments/core.py" (relative to repo root)
    line_number: int     # where the function starts, useful for the UI later
    calls: list = field(default_factory=list)  # names of functions THIS function calls


@dataclass
class FileImports:
    """
    Represents what one file imports from elsewhere in the repo.
    This becomes the "import edges" in our graph later.

    WHY "imports" IS A LIST OF (module_string, relative_level) TUPLES,
    NOT JUST A LIST OF STRINGS:
    Python has two import styles that need different resolution logic:
      - Absolute: "from flask.ctx import x"   -> module_string="flask.ctx", level=0
      - Relative: "from . import ctx"          -> module_string="ctx",       level=1
      - Relative: "from .. import helpers"     -> module_string="helpers",   level=2
    level=0 means "resolve this from the repo root" (absolute import).
    level=1 means "resolve this relative to MY OWN folder" (one dot).
    level=2 means "resolve this relative to my PARENT folder" (two dots).
    Without tracking the level, "ctx" on its own is ambiguous, we can't tell
    if it means the top-level file "ctx.py" or "flask/ctx.py" relative to
    whoever's importing it. The graph builder needs this level to resolve
    the import to the correct actual file.
    """
    filepath: str
    imports: list = field(default_factory=list)  # list of (module_string, relative_level) tuples


def find_python_files(repo_path: str) -> list:
    """
    Walks the entire repo folder and returns a list of all .py file paths.

    WHY WE SKIP CERTAIN FOLDERS:
    Repos often contain virtual environments, build artifacts, or test
    fixtures that aren't "real" source code for our purposes. We skip common
    junk folders so we don't waste time parsing thousands of irrelevant files
    (this also avoids double-counting vendored/copied dependency code as if
    it were part of the project's own architecture).
    """
    SKIP_DIRS = {".git", "venv", ".venv", "env", "__pycache__", "node_modules", "build", "dist"}

    python_files = []
    for root, dirs, files in os.walk(repo_path):
        # Modifying `dirs` in-place like this tells os.walk() to not descend
        # into these folders at all, it's more efficient than walking in and
        # then ignoring the results.
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for filename in files:
            if filename.endswith(".py"):
                full_path = os.path.join(root, filename)
                python_files.append(full_path)

    return python_files


def parse_file(filepath: str, repo_root: str) -> tuple:
    """
    Parses a single Python file and extracts:
      - every function definition in it
      - every import statement in it

    Returns a tuple: (list of FunctionDef, FileImports)

    WHY WE WRAP THIS IN A TRY/EXCEPT:
    Real-world repos sometimes contain Python files with syntax errors
    (e.g. Python 2 code in a Python 3 repo, or a file that's auto-generated
    and malformed). If one file fails to parse, we don't want the entire
    analysis to crash, we want to skip that file and keep going, then report
    it at the end.
    """
    relative_path = os.path.relpath(filepath, repo_root)

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        source_code = f.read()

    try:
        tree = ast.parse(source_code, filename=filepath)
    except SyntaxError as e:
        print(f"  [skipped, syntax error] {relative_path}: {e}")
        return [], FileImports(filepath=relative_path)

    functions_found = []
    imports_found = []

    # ast.walk() visits every node in the tree, not just the top level.
    # This is how we find functions defined inside classes too, not just
    # ones at the top of the file.
    for node in ast.walk(tree):

        # Case 1: a function or method definition (e.g. "def process_payment():")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls_in_this_function = _find_calls_inside(node)
            functions_found.append(
                FunctionDef(
                    name=node.name,
                    filepath=relative_path,
                    line_number=node.lineno,
                    calls=calls_in_this_function,
                )
            )

        # Case 2: a plain import statement (e.g. "import os" or "import payments.core")
        # "import" statements are always absolute (level 0), Python has no
        # syntax for a relative plain "import", only "from . import x" uses
        # relative levels.
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports_found.append((alias.name, 0))

        # Case 3: a "from X import Y" statement, which can be absolute
        # ("from payments.core import process_payment", level=0) or relative
        # ("from . import ctx", level=1, or "from .. import x", level=2).
        # node.module can be None for "from . import x" (no module name at
        # all, just dots), in which case we still need to record the level
        # even though module_string is empty, otherwise we'd silently drop
        # bare relative imports entirely.
        elif isinstance(node, ast.ImportFrom):
            module_string = node.module if node.module else ""
            imports_found.append((module_string, node.level))

    return functions_found, FileImports(filepath=relative_path, imports=imports_found)


def _find_calls_inside(function_node) -> list:
    """
    Given an AST node for a single function, finds every function call made
    INSIDE that function's body.

    Example: if process_payment() contains the line `validate_card(card)`,
    this returns ["validate_card"].

    WHY THIS IS ITS OWN HELPER FUNCTION (prefixed with _ to signal it's
    internal/private to this file):
    We need to walk the function's body specifically, not the whole file,
    otherwise we'd attribute every call in the file to every function.
    """
    calls = []
    for node in ast.walk(function_node):
        if isinstance(node, ast.Call):
            # A call's "func" can be a simple name (validate_card(...))
            # or an attribute access (self.validate_card(...) or db.save(...)).
            # We handle both cases so we don't miss method calls.
            if isinstance(node.func, ast.Name):
                calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)
    return calls


def parse_repo(repo_path: str) -> dict:
    """
    The main entry point for this file. Parses every Python file in the repo
    and returns a single dictionary with everything we found.

    Returns:
        {
            "functions": [list of FunctionDef across the whole repo],
            "file_imports": [list of FileImports across the whole repo],
            "files_parsed": int,
            "files_skipped": int,
        }
    """
    python_files = find_python_files(repo_path)
    print(f"Found {len(python_files)} Python files. Parsing...")

    all_functions = []
    all_file_imports = []
    skipped_count = 0

    for filepath in python_files:
        functions, file_imports = parse_file(filepath, repo_path)
        if not functions and not file_imports.imports:
            # could be a genuinely empty file, or a skipped/broken one;
            # we don't distinguish here, just note it for a sanity check later
            pass
        all_functions.extend(functions)
        all_file_imports.append(file_imports)

    print(f"Parsing complete. Found {len(all_functions)} function definitions "
          f"across {len(python_files)} files.")

    return {
        "functions": all_functions,
        "file_imports": all_file_imports,
        "files_parsed": len(python_files),
        "files_skipped": skipped_count,
    }


# --- Quick manual test ---
if __name__ == "__main__":
    from clone_repo import clone_repo, cleanup_repo

    repo_path = clone_repo("https://github.com/pallets/flask")
    result = parse_repo(repo_path)

    print("\n--- Sample of what we found ---")
    for func in result["functions"][:5]:
        print(f"  {func.filepath}:{func.line_number}  def {func.name}()  calls: {func.calls[:3]}")

    print("\n--- Sample imports ---")
    for fi in result["file_imports"][:5]:
        if fi.imports:
            print(f"  {fi.filepath} imports: {fi.imports[:3]}")  # each item is (module_string, level)

    cleanup_repo(repo_path)