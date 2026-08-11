"""AST-based parser that turns the `checkout_service` sample project into a code graph.

Walks every `.py` file in `sample_project/checkout_service/`, and for each module,
class, and function/method emits a `code_nodes` row (per `sql/schema.sql`) plus
best-effort `code_edges` rows for `imports` and `calls` relationships.

Stdlib only (`ast`, `uuid`, `os`, `pathlib`). Pure parse — no DB connection, no
embedding, no network calls. Designed to be imported by a loader later via
`parse_project()`; running this file directly prints a human-readable summary.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

# Fixed namespace so uuid5-derived node ids are stable across re-runs.
NAMESPACE = uuid.UUID("6f6a6e46-2c1a-4b8b-9a3d-2f6e6b7d9c11")

SERVICE_NAME = "checkout-service"
PACKAGE_NAME = "checkout_service"


# ── small helpers ────────────────────────────────────────────────────────────


def _node_id(service: str, file: str, kind: str, qualified_name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{service}:{file}:{kind}:{qualified_name}"))


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


def _discover_modules(package_dir: Path, base_dir: Path) -> list[_Module]:
    modules = []
    for py_file in sorted(package_dir.glob("*.py")):
        source = py_file.read_text()
        tree = ast.parse(source, filename=str(py_file))
        stem = py_file.stem
        module_name = PACKAGE_NAME if stem == "__init__" else f"{PACKAGE_NAME}.{stem}"
        rel_file = str(py_file.relative_to(base_dir))
        modules.append(_Module(module_name, py_file, rel_file, tree, source))
    return modules


# ── main parse ────────────────────────────────────────────────────────────────


def parse_project(root: str) -> tuple[list[dict], list[dict]]:
    """Parse the `checkout_service` package under `root` into (nodes, edges).

    `root` is the path to the directory that *contains* `checkout_service/`
    (i.e. the `sample_project` directory), so emitted `file` paths come out as
    `sample_project/checkout_service/<name>.py`.
    """
    root_path = Path(root).resolve()
    package_dir = root_path / PACKAGE_NAME
    base_dir = root_path.parent  # so rel_file includes "sample_project/..."

    modules = _discover_modules(package_dir, base_dir)
    module_by_name = {m.module_name: m for m in modules}

    nodes: list[dict] = []

    # registries used for best-effort call resolution. Names happen to be
    # unique across this small project, so we key globally rather than
    # per-module (documented limitation — see report).
    classes: dict[str, dict] = {}      # class name -> {id, qualname, module, file, node}
    functions: dict[str, dict] = {}    # function name -> {id, qualname, module, file, node, return_type}
    methods: dict[str, dict] = {}      # "Class.method" -> {id, qualname, module, file, node, class, return_type}
    module_nodes: dict[str, dict] = {}  # module_name -> {id, qualname, file}

    # ── pass 1: build module/class/function/method nodes ────────────────────
    for m in modules:
        mod_node = {
            "id": _node_id(SERVICE_NAME, m.rel_file, "module", m.module_name),
            "name": m.module_name,
            "kind": "module",
            "file": m.rel_file,
            "service": SERVICE_NAME,
            "source": m.source,
            "summary": _docstring_summary(m.tree),
            "last_commit": None,
        }
        nodes.append(mod_node)
        module_nodes[m.module_name] = mod_node

        for stmt in m.tree.body:
            if isinstance(stmt, ast.ClassDef):
                cls_qualname = stmt.name
                cls_id = _node_id(SERVICE_NAME, m.rel_file, "class", cls_qualname)
                cls_node = {
                    "id": cls_id,
                    "name": cls_qualname,
                    "kind": "class",
                    "file": m.rel_file,
                    "service": SERVICE_NAME,
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
                        method_id = _node_id(SERVICE_NAME, m.rel_file, "function", method_qualname)
                        method_node = {
                            "id": method_id,
                            "name": method_qualname,
                            "kind": "function",
                            "file": m.rel_file,
                            "service": SERVICE_NAME,
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
                func_id = _node_id(SERVICE_NAME, m.rel_file, "function", func_qualname)
                func_node = {
                    "id": func_id,
                    "name": func_qualname,
                    "kind": "function",
                    "file": m.rel_file,
                    "service": SERVICE_NAME,
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
