"""AST-based parser that turns a Python project into a code graph.

For each module, class, and function/method it emits a `code_nodes`-shaped dict
(per `sql/schema.sql`) plus best-effort `code_edges` dicts for `imports` and
`calls` relationships. Two entry points share one core (`_build_graph`):

- `parse_project(root)` — the built-in `checkout_service` sample (top-level
  package glob; files reported as `sample_project/checkout_service/<name>.py`).
- `parse_python_project(root, project, service)` — an ARBITRARY onboarded repo:
  a recursive `.py` walk rooted at the project directory (root-relative file
  paths), skipping vendor/build noise and tolerating unparseable files.

Node ids are `uuid5(project:service:file:kind:qualname)` — folding `project`
in namespaces the graph per tenant, so two onboarded projects can't collide and
re-syncing one upserts in place. Stdlib only (`ast`, `uuid`, `pathlib`): pure
parse — no DB connection, no embedding, no network, and the target code is never
imported or executed.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

# Fixed namespace so uuid5-derived node ids are stable across re-runs.
NAMESPACE = uuid.UUID("6f6a6e46-2c1a-4b8b-9a3d-2f6e6b7d9c11")

# The built-in demo's identifiers (parse_project's defaults).
SERVICE_NAME = "checkout-service"
PACKAGE_NAME = "checkout_service"
SAMPLE_PROJECT = "sample"

# Directories never worth walking in an arbitrary repo (vendored deps, VCS
# metadata, build artifacts, virtualenvs, tool caches).
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn",
        "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".venv", "venv", "env", ".env", "virtualenv",
        "node_modules", "site-packages", ".eggs", ".tox", ".nox",
        "build", "dist", ".next", ".cache",
    }
)


# ── small helpers ────────────────────────────────────────────────────────────


def _node_id(project: str, service: str, file: str, kind: str, qualified_name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{project}:{service}:{file}:{kind}:{qualified_name}"))


def _docstring_summary(node: ast.AST) -> str | None:
    doc = ast.get_docstring(node)
    if not doc:
        return None
    first_line = doc.strip().splitlines()[0].strip()
    return first_line or None


def _dfs(node: ast.AST):
    """Pre-order depth-first traversal that follows AST field order (source order)."""
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _dfs(child)


# ── module discovery ─────────────────────────────────────────────────────────


class _Module:
    def __init__(self, module_name, file_path: Path, rel_file: str, tree: ast.Module, source: str):
        self.module_name = module_name
        self.file_path = file_path
        self.rel_file = rel_file
        self.tree = tree
        self.source = source


def _discover_package(package_dir: Path, base_dir: Path, package_name: str) -> list[_Module]:
    """Top-level `.py` files of one package (the sample-project layout)."""
    modules = []
    for py_file in sorted(package_dir.glob("*.py")):
        source = py_file.read_text()
        tree = ast.parse(source, filename=str(py_file))
        stem = py_file.stem
        module_name = package_name if stem == "__init__" else f"{package_name}.{stem}"
        rel_file = str(py_file.relative_to(base_dir))
        modules.append(_Module(module_name, py_file, rel_file, tree, source))
    return modules


def _discover_recursive(root: Path, max_files: int | None = None) -> list[_Module]:
    """Every `.py` file under `root`, recursively — the arbitrary-repo path.

    Skips vendor/build/VCS noise (`SKIP_DIRS`) and tolerates files that can't be
    read or parsed (binary-ish, non-utf8, Python 2, syntax errors): those are
    simply omitted rather than aborting the whole ingest. Module names are the
    dotted relative path (``pkg/sub/mod.py`` -> ``pkg.sub.mod``; a package
    ``__init__.py`` collapses to its directory)."""
    modules: list[_Module] = []
    for py_file in sorted(root.rglob("*.py")):
        rel = py_file.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary/unreadable — skip
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue  # Python 2 / broken file — skip
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        module_name = ".".join(parts) if parts else py_file.stem
        modules.append(_Module(module_name, py_file, str(rel), tree, source))
        if max_files is not None and len(modules) >= max_files:
            break
    return modules


# ── public entry points ────────────────────────────────────────────────────────


def parse_project(root: str, *, project: str = SAMPLE_PROJECT) -> tuple[list[dict], list[dict]]:
    """Parse the `checkout_service` package under `root` into (nodes, edges).

    `root` is the path to the directory that *contains* `checkout_service/`
    (i.e. the `sample_project` directory), so emitted `file` paths come out as
    `sample_project/checkout_service/<name>.py`. Backward-compatible entry for
    the built-in demo; arbitrary repos use `parse_python_project`.
    """
    root_path = Path(root).resolve()
    package_dir = root_path / PACKAGE_NAME
    base_dir = root_path.parent  # so rel_file includes "sample_project/..."
    modules = _discover_package(package_dir, base_dir, PACKAGE_NAME)
    return _build_graph(modules, project=project, service=SERVICE_NAME)


def parse_python_project(
    root: str,
    *,
    project: str,
    service: str,
    max_files: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Parse an ARBITRARY Python repo rooted at `root` into (nodes, edges).

    Recursively walks `*.py` (root-relative file paths), scoping every node id
    to `project`/`service` so onboarded repos never collide with each other or
    the demo. `max_files` bounds very large repos (None = unbounded).
    """
    root_path = Path(root).resolve()
    modules = _discover_recursive(root_path, max_files=max_files)
    return _build_graph(modules, project=project, service=service)


# ── core graph builder ─────────────────────────────────────────────────────────


def _build_graph(modules: list[_Module], *, project: str, service: str) -> tuple[list[dict], list[dict]]:
    """Turn discovered modules into (nodes, edges). Shared by both entry points."""
    module_by_name = {m.module_name: m for m in modules}

    nodes: list[dict] = []

    # registries used for best-effort call resolution. Names are keyed globally
    # (not per-module); across a small project they're unique. In a large repo
    # collisions can mis-resolve a call edge — a documented best-effort limit.
    classes: dict[str, dict] = {}      # class name -> {id, qualname, module, file, node}
    functions: dict[str, dict] = {}    # function name -> {id, qualname, module, file, node, return_type}
    methods: dict[str, dict] = {}      # "Class.method" -> {id, qualname, module, file, node, class, return_type}
    module_nodes: dict[str, dict] = {}  # module_name -> {id, qualname, file}

    # ── pass 1: build module/class/function/method nodes ────────────────────
    for m in modules:
        mod_node = {
            "id": _node_id(project, service, m.rel_file, "module", m.module_name),
            "name": m.module_name,
            "kind": "module",
            "file": m.rel_file,
            "service": service,
            "source": m.source,
            "summary": _docstring_summary(m.tree),
            "last_commit": None,
        }
        nodes.append(mod_node)
        module_nodes[m.module_name] = mod_node

        for stmt in m.tree.body:
            if isinstance(stmt, ast.ClassDef):
                cls_qualname = stmt.name
                cls_id = _node_id(project, service, m.rel_file, "class", cls_qualname)
                cls_node = {
                    "id": cls_id,
                    "name": cls_qualname,
                    "kind": "class",
                    "file": m.rel_file,
                    "service": service,
                    "source": ast.get_source_segment(m.source, stmt),
                    "summary": _docstring_summary(stmt),
                    "last_commit": None,
                }
                nodes.append(cls_node)
                classes[stmt.name] = {
                    "id": cls_id,
                    "qualname": cls_qualname,
                    "module": m.module_name,
                    "file": m.rel_file,
                    "node": stmt,
                }

                for sub in stmt.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_qualname = f"{stmt.name}.{sub.name}"
                        method_id = _node_id(project, service, m.rel_file, "function", method_qualname)
                        method_node = {
                            "id": method_id,
                            "name": method_qualname,
                            "kind": "function",
                            "file": m.rel_file,
                            "service": service,
                            "source": ast.get_source_segment(m.source, sub),
                            "summary": _docstring_summary(sub),
                            "last_commit": None,
                        }
                        nodes.append(method_node)
                        methods[method_qualname] = {
                            "id": method_id,
                            "qualname": method_qualname,
                            "module": m.module_name,
                            "file": m.rel_file,
                            "node": sub,
                            "class": stmt.name,
                            "return_type": None,
                        }

            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_qualname = stmt.name
                func_id = _node_id(project, service, m.rel_file, "function", func_qualname)
                func_node = {
                    "id": func_id,
                    "name": func_qualname,
                    "kind": "function",
                    "file": m.rel_file,
                    "service": service,
                    "source": ast.get_source_segment(m.source, stmt),
                    "summary": _docstring_summary(stmt),
                    "last_commit": None,
                }
                nodes.append(func_node)
                functions[stmt.name] = {
                    "id": func_id,
                    "qualname": func_qualname,
                    "module": m.module_name,
                    "file": m.rel_file,
                    "node": stmt,
                    "return_type": None,
                }

    # ── pass 2: module-level singleton var types, e.g. `_POOL = ConnectionPool()` ──
    global_var_types: dict[str, str] = {}
    for m in modules:
        for stmt in m.tree.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id in classes
            ):
                global_var_types[stmt.targets[0].id] = stmt.value.func.id

    def infer_value_class(call: ast.Call, local_types: dict, self_attr_types: dict, current_class: str | None) -> str | None:
        """Best-effort: what class does the *result* of this Call belong to?"""
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in classes:
                return func.id
            if func.id in functions:
                return functions[func.id]["return_type"]
            if func.id in global_var_types:
                return global_var_types[func.id]
        elif isinstance(func, ast.Attribute):
            attr = func.attr
            if attr in functions:
                return functions[attr]["return_type"]
            obj_class = resolve_object_class(func.value, local_types, self_attr_types, current_class)
            if obj_class:
                key = f"{obj_class}.{attr}"
                if key in methods:
                    return methods[key]["return_type"]
        return None

    def resolve_object_class(expr: ast.AST, local_types: dict, self_attr_types: dict, current_class: str | None) -> str | None:
        if isinstance(expr, ast.Name):
            if expr.id == "self":
                return current_class
            return local_types.get(expr.id)
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == "self":
            return self_attr_types.get(current_class, {}).get(expr.attr)
        return None

    # ── pass 3: infer return types for module functions and methods ─────────
    def compute_return_type(func_def: ast.AST) -> str | None:
        for node in _dfs(func_def):
            if isinstance(node, ast.Return) and node.value is not None:
                val = node.value
                if isinstance(val, ast.Name) and val.id in global_var_types:
                    return global_var_types[val.id]
                if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) and val.func.id in classes:
                    return val.func.id
                return None  # first return statement decides; unresolvable
        return None

    for fname, info in functions.items():
        info["return_type"] = compute_return_type(info["node"])
    for mname, info in methods.items():
        info["return_type"] = compute_return_type(info["node"])

    # ── pass 4: self.attr = <Class instance | getter()> inside any method ───
    self_attr_types: dict[str, dict[str, str]] = {}
    for mname, info in methods.items():
        cls = info["class"]
        self_attr_types.setdefault(cls, {})
        for node in _dfs(info["node"]):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Attribute)
                and isinstance(node.targets[0].value, ast.Name)
                and node.targets[0].value.id == "self"
                and isinstance(node.value, ast.Call)
            ):
                resolved = infer_value_class(node.value, {}, self_attr_types, cls)
                if resolved:
                    self_attr_types[cls][node.targets[0].attr] = resolved

    # ── pass 5: import edges (module -> module) ──────────────────────────────
    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def add_edge(src_id: str, dst_id: str, kind: str) -> None:
        key = (src_id, dst_id, kind)
        if key in seen_edges or src_id == dst_id:
            return
        seen_edges.add(key)
        edges.append({"src_id": src_id, "dst_id": dst_id, "kind": kind})

    for m in modules:
        src_id = module_nodes[m.module_name]["id"]
        for stmt in m.tree.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.name in module_nodes:
                        add_edge(src_id, module_nodes[alias.name]["id"], "imports")
            elif isinstance(stmt, ast.ImportFrom):
                base = stmt.module or ""
                for alias in stmt.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    if candidate in module_nodes:
                        add_edge(src_id, module_nodes[candidate]["id"], "imports")
                    elif base in module_nodes:
                        add_edge(src_id, module_nodes[base]["id"], "imports")

    # ── pass 6: call edges ────────────────────────────────────────────────────
    def resolve_call_target(call: ast.Call, local_types: dict, current_class: str | None) -> dict | None:
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in classes:
                return classes[func.id]
            if func.id in functions:
                return functions[func.id]
            return None
        if isinstance(func, ast.Attribute):
            attr = func.attr
            value = func.value
            if isinstance(value, ast.Name) and value.id == "self":
                key = f"{current_class}.{attr}"
                return methods.get(key)
            obj_class = resolve_object_class(value, local_types, self_attr_types, current_class)
            if obj_class:
                key = f"{obj_class}.{attr}"
                if key in methods:
                    return methods[key]
            # fall back: module.function() style call (module alias not tracked,
            # but function names are unique in this project)
            if obj_class is None and attr in functions:
                return functions[attr]
            return None
        return None

    def process_callable(current_id: str, current_class: str | None, func_def: ast.AST) -> None:
        local_types: dict[str, str] = {}
        for node in _dfs(func_def):
            if isinstance(node, ast.Call):
                target = resolve_call_target(node, local_types, current_class)
                if target:
                    add_edge(current_id, target["id"], "calls")
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
            ):
                resolved = infer_value_class(node.value, local_types, self_attr_types, current_class)
                if resolved:
                    local_types[node.targets[0].id] = resolved

    for fname, info in functions.items():
        process_callable(info["id"], None, info["node"])
    for mname, info in methods.items():
        process_callable(info["id"], info["class"], info["node"])

    return nodes, edges


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[2] / "sample_project"
    nodes, edges = parse_project(str(project_root))

    id_to_name = {n["id"]: n["name"] for n in nodes}

    print(f"=== code graph for {SERVICE_NAME} ===")
    print(f"root: {project_root}")
    print(f"nodes: {len(nodes)}  edges: {len(edges)}")
    print()

    by_kind: dict[str, int] = {}
    for n in nodes:
        by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
    print("node counts by kind:", by_kind)
    print()

    print("-- nodes (name | kind | file) --")
    for n in nodes:
        print(f"{n['name']} | {n['kind']} | {n['file']}")
    print()

    print("-- edges --")
    for e in edges:
        src_name = id_to_name.get(e["src_id"], e["src_id"])
        dst_name = id_to_name.get(e["dst_id"], e["dst_id"])
        print(f"{src_name} --{e['kind']}--> {dst_name}")
