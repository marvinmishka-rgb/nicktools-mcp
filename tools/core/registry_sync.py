"""Introspect the tool registry, extract metadata from tool files, validate docs.

Scans TOOL_REGISTRY + all tool .py files to build a complete manifest.
Auto-extracts: function signatures, imports, lib dependencies, docstrings.
Reads optional YAML frontmatter from tool docstrings for metadata that
can't be inferred (creates_nodes, creates_edges, databases).
Validates USAGE.md has a section for every registered operation.
Generates manifest.yaml with the full picture.

Designed as in-process tool (_impl function) for fast dispatch.
---
description: Validate tool registry, extract metadata, check USAGE.md completeness
databases: []
---
"""
import ast
import inspect
import re
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _parse_frontmatter(docstring):
    """Extract YAML frontmatter from a module or function docstring.

    Frontmatter is delimited by --- markers within the docstring:
        '''Some description text.
        ---
        key: value
        list_key: [a, b, c]
        ---
        '''

    Returns dict of parsed key-value pairs, or empty dict.
    """
    if not docstring:
        return {}

    # Find content between --- markers
    match = re.search(r'---\s*\n(.*?)\n\s*---', docstring, re.DOTALL)
    if not match:
        return {}

    frontmatter = {}
    for line in match.group(1).strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip()
        value = value.strip()

        # Parse YAML-ish values
        if value.startswith('[') and value.endswith(']'):
            # List: [a, b, c]
            items = [v.strip().strip('"').strip("'")
                     for v in value[1:-1].split(',') if v.strip()]
            frontmatter[key] = items
        elif value.lower() in ('true', 'false'):
            frontmatter[key] = value.lower() == 'true'
        elif value.isdigit():
            frontmatter[key] = int(value)
        else:
            frontmatter[key] = value

    return frontmatter


def _extract_func_params(node):
    """Extract parameters from a function/async function AST node.

    Returns list of {name, default, required} dicts.
    Skips self, cls, driver, kwargs, attempt.
    """
    params = []
    args = node.args
    num_defaults = len(args.defaults)
    num_args = len(args.args)

    for i, arg in enumerate(args.args):
        if arg.arg in ('self', 'cls', 'driver', 'kwargs', 'attempt'):
            continue
        param = {"name": arg.arg, "required": True, "default": None}

        default_idx = i - (num_args - num_defaults)
        if default_idx >= 0:
            default = args.defaults[default_idx]
            param["required"] = False
            if isinstance(default, ast.Constant):
                param["default"] = default.value
            elif isinstance(default, ast.NameConstant):
                param["default"] = default.value
            elif isinstance(default, ast.Str):
                param["default"] = default.s
            elif isinstance(default, ast.Num):
                param["default"] = default.n
            else:
                param["default"] = "..."

        params.append(param)
    return params


def _extract_params_from_load_params(source):
    """Extract params from load_params() usage patterns in source code.

    Finds patterns like:
        params["url"]  or  p["url"]          -> required param
        params.get("domain")                  -> optional param (default None)
        params.get("wait_seconds", 8)         -> optional param with default
        p.get("cache_ttl", BROWSE_CACHE_TTL)  -> optional param (default "...")

    Returns list of {name, default, required} dicts.
    """
    params = []
    seen = set()

    # Required: p["key"] or params["key"] (but NOT query_params, cypher_params, etc.)
    for m in re.finditer(r'(?<![a-zA-Z_])(?:params|p)\["(\w+)"\]', source):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            params.append({"name": name, "required": True, "default": None})

    # Optional: p.get("key") or params.get("key", default) (but NOT query_params.get, etc.)
    for m in re.finditer(r'(?<![a-zA-Z_])(?:params|p)\.get\("(\w+)"(?:\s*,\s*([^)]+))?\)', source):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            default_str = m.group(2)
            if default_str is None:
                default = None
            else:
                default_str = default_str.strip()
                # Try to parse literal defaults
                if default_str in ('True', 'False'):
                    default = default_str == 'True'
                elif default_str == 'None':
                    default = None
                elif default_str.isdigit():
                    default = int(default_str)
                elif default_str.startswith('"') or default_str.startswith("'"):
                    default = default_str.strip('"').strip("'")
                elif default_str.startswith('['):
                    default = []
                else:
                    default = "..."  # constant reference, can't resolve
            params.append({"name": name, "required": False, "default": default})

    return params


def _extract_tool_metadata(tool_path):
    """Parse a tool .py file and extract metadata via AST.

    Uses a three-strategy approach:
    1. Look for *_impl() functions (in-process tools, 27 of 31)
    2. Look for the primary async function (subprocess tools with proper signatures)
    3. Fall back to scanning load_params() usage patterns (subprocess tools with
       module-level param loading)

    Returns dict with:
        module_doc: module docstring
        frontmatter: parsed YAML frontmatter from docstring
        impl_func: name of the primary function (if found)
        params: list of {name, default, required} from function signature or load_params
        imports: list of import strings
        lib_imports: list of lib.* module imports
        uses_canonicalize: bool
        uses_neo4j_driver: bool
        uses_wire_supported_by: bool
        uses_wire_cites: bool
    """
    try:
        source = tool_path.read_text(encoding='utf-8')
        tree = ast.parse(source)
    except Exception as e:
        return {"error": f"Parse error: {e}"}

    result = {
        "file": str(tool_path),
        "module_doc": "",
        "frontmatter": {},
        "impl_func": None,
        "params": [],
        "imports": [],
        "lib_imports": [],
        "uses_canonicalize": False,
        "uses_neo4j_driver": False,
        "uses_wire_supported_by": False,
        "uses_wire_cites": False,
    }

    # Module docstring
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, (ast.Str, ast.Constant))):
        doc = tree.body[0].value
        result["module_doc"] = doc.s if isinstance(doc, ast.Str) else str(doc.value)
        result["frontmatter"] = _parse_frontmatter(result["module_doc"])

    # Collect all function defs (both sync and async) for multi-strategy search
    impl_func = None
    async_funcs = []
    named_funcs = []

    # Walk the AST for imports and function definitions
    for node in ast.walk(tree):
        # Imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                result["imports"].append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                full = f"{module}.{alias.name}"
                result["imports"].append(full)
                if module.startswith("lib."):
                    result["lib_imports"].append(full)
                if alias.name == "canonicalize_url":
                    result["uses_canonicalize"] = True
                if alias.name == "get_neo4j_driver":
                    result["uses_neo4j_driver"] = True
                if alias.name == "wire_supported_by":
                    result["uses_wire_supported_by"] = True
                if alias.name == "wire_cites_edges":
                    result["uses_wire_cites"] = True

        # Collect function definitions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.endswith("_impl"):
                impl_func = node
            elif isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
                async_funcs.append(node)
            elif node.name == "main":
                named_funcs.append(node)

    # Strategy 1: *_impl function (in-process tools)
    if impl_func:
        result["impl_func"] = impl_func.name
        result["params"] = _extract_func_params(impl_func)

        # Function docstring
        if (impl_func.body and isinstance(impl_func.body[0], ast.Expr)
                and isinstance(impl_func.body[0].value, (ast.Str, ast.Constant))):
            doc = impl_func.body[0].value
            result["func_doc"] = doc.s if isinstance(doc, ast.Str) else str(doc.value)

    # Strategy 2: Primary async function (subprocess tools with proper signatures)
    elif async_funcs:
        # Pick the async function with the most params (most likely the real entry point)
        best = max(async_funcs, key=lambda f: len(f.args.args))
        params = _extract_func_params(best)
        if params:  # Only use if it actually has meaningful params
            result["impl_func"] = best.name
            result["params"] = params
            if (best.body and isinstance(best.body[0], ast.Expr)
                    and isinstance(best.body[0].value, (ast.Str, ast.Constant))):
                doc = best.body[0].value
                result["func_doc"] = doc.s if isinstance(doc, ast.Str) else str(doc.value)

    # Strategy 3: Scan load_params() usage patterns (fallback for all subprocess tools)
    if not result["params"]:
        lp_params = _extract_params_from_load_params(source)
        if lp_params:
            result["params"] = lp_params
            result["impl_func"] = result["impl_func"] or "(load_params)"

    return result


def _check_usage_md(group_name, operations, tools_dir):
    """Check if USAGE.md has sections for all operations in a group.

    Returns list of operations missing from USAGE.md.
    """
    # Map group names to directories
    dir_map = {
        "graph": "graph",
        "research": "research",
        "entry": "workflow",
        "core": "core",
    }
    subdir = dir_map.get(group_name, group_name)
    usage_path = tools_dir / subdir / "USAGE.md"

    if not usage_path.exists():
        return list(operations.keys()), f"USAGE.md not found: {usage_path}"

    usage_text = usage_path.read_text(encoding='utf-8')
    missing = []
    for op_name in operations:
        # Check for a ## section header matching the operation name
        pattern = rf'##\s+{re.escape(op_name)}\b'
        if not re.search(pattern, usage_text):
            missing.append(op_name)

    return missing, str(usage_path)


def registry_sync_impl(action="validate", output_path=None, driver=None, **kwargs):
    """Introspect tool registry, extract metadata, validate docs.

    Args:
        action: What to do:
            "validate" -- check USAGE.md completeness, report drift (default)
            "manifest" -- generate full manifest.yaml with all metadata
            "report" -- human-readable summary of all tools
        output_path: Where to write manifest (default: STRUCTURE manifest path)
        driver: Unused (absorbed for compatibility)

    Returns:
        dict with validation results, manifest data, or report text
    """
    # Import TOOL_REGISTRY from server.py
    server_dir = Path(__file__).resolve().parent.parent.parent
    tools_dir = server_dir / "tools"

    # We need to read TOOL_REGISTRY without importing server.py
    # (which would start FastMCP). Parse it from the source.
    server_path = server_dir / "server.py"
    server_source = server_path.read_text(encoding='utf-8')

    # Extract SUBPROCESS_ONLY set
    subprocess_match = re.search(
        r'SUBPROCESS_ONLY\s*=\s*\{([^}]+)\}', server_source, re.DOTALL
    )
    subprocess_tools = set()
    if subprocess_match:
        for m in re.finditer(r'"([^"]+)"', subprocess_match.group(1)):
            subprocess_tools.add(m.group(1))

    # Parse TOOL_REGISTRY structure
    # Extract group names and their operations from the source
    registry = {}
    # Use regex to find group blocks
    group_pattern = re.compile(
        r'"(\w+)":\s*\{\s*"usage_file":\s*"([^"]+)".*?"operations":\s*\{(.*?)\}\s*\}',
        re.DOTALL
    )
    for group_match in group_pattern.finditer(server_source):
        group_name = group_match.group(1)
        usage_file = group_match.group(2)
        ops_block = group_match.group(3)

        operations = {}
        op_pattern = re.compile(
            r'"(\w+)":\s*\{([^}]+)\}',
        )
        for op_match in op_pattern.finditer(ops_block):
            op_name = op_match.group(1)
            op_config_str = op_match.group(2)

            # Extract script path
            script_match = re.search(r'"script":\s*"([^"]+)"', op_config_str)
            script = script_match.group(1) if script_match else ""

            # Extract timeouts
            timeout_match = re.search(r'"timeout":\s*(\d+)', op_config_str)
            max_timeout_match = re.search(r'"max_timeout":\s*(\d+)', op_config_str)

            operations[op_name] = {
                "script": script,
                "timeout": int(timeout_match.group(1)) if timeout_match else None,
                "max_timeout": int(max_timeout_match.group(1)) if max_timeout_match else None,
                "dispatch": "subprocess" if script in subprocess_tools else "in-process",
                "has_preprocess": "preprocess" in op_config_str,
            }

        registry[group_name] = {
            "usage_file": usage_file,
            "operations": operations,
        }

    # Now scan all tool .py files
    tool_metadata = {}
    for group_name, group_info in registry.items():
        for op_name, op_config in group_info["operations"].items():
            script_path = tools_dir / op_config["script"]
            if script_path.exists():
                meta = _extract_tool_metadata(script_path)
                meta["group"] = group_name
                meta["operation"] = op_name
                meta["dispatch"] = op_config["dispatch"]
                meta["timeout"] = op_config["timeout"]
                meta["max_timeout"] = op_config["max_timeout"]
                meta["has_preprocess"] = op_config["has_preprocess"]
                tool_metadata[op_name] = meta
            else:
                tool_metadata[op_name] = {
                    "error": f"Script not found: {script_path}",
                    "group": group_name,
                    "operation": op_name,
                }

    # Validate USAGE.md completeness
    usage_issues = []
    for group_name, group_info in registry.items():
        missing, usage_path = _check_usage_md(
            group_name, group_info["operations"], tools_dir
        )
        if missing:
            usage_issues.append({
                "group": group_name,
                "usage_file": usage_path,
                "missing_sections": missing,
            })

    # Check for tools that create Source nodes but don't use canonicalize_url
    canonicalization_warnings = []
    source_creating_patterns = ["Source", "MERGE.*Source", "source.*url"]
    for op_name, meta in tool_metadata.items():
        if meta.get("error"):
            continue
        # Check if tool mentions Source node creation in its source
        script_path = Path(meta["file"])
        source_text = script_path.read_text(encoding='utf-8')
        creates_sources = bool(re.search(r'MERGE\s*\(.*:Source', source_text))
        if creates_sources and not meta.get("uses_canonicalize"):
            canonicalization_warnings.append({
                "operation": op_name,
                "group": meta["group"],
                "file": meta["file"],
                "issue": "Creates Source nodes via MERGE but does not import canonicalize_url",
            })

    # Check for tools with frontmatter vs without
    frontmatter_status = {
        "with_frontmatter": [],
        "without_frontmatter": [],
    }
    for op_name, meta in tool_metadata.items():
        if meta.get("error"):
            continue
        if meta.get("frontmatter"):
            frontmatter_status["with_frontmatter"].append(op_name)
        else:
            frontmatter_status["without_frontmatter"].append(op_name)

    # Compute counts for header validation
    counts = {}
    for group_name, group_info in registry.items():
        counts[group_name] = len(group_info["operations"])
    total = sum(counts.values())

    if action == "validate":
        result = {
            "status": "ok" if not usage_issues and not canonicalization_warnings else "drift_detected",
            "tool_counts": counts,
            "total_operations": total,
            "usage_md_issues": usage_issues if usage_issues else "all sections present",
            "canonicalization_warnings": canonicalization_warnings if canonicalization_warnings else "all Source-creating tools use canonicalize_url",
            "frontmatter_coverage": {
                "with": len(frontmatter_status["with_frontmatter"]),
                "without": len(frontmatter_status["without_frontmatter"]),
                "missing": frontmatter_status["without_frontmatter"][:10],  # first 10
            },
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        return result

    elif action == "manifest":
        # Load previous manifest to detect structural changes
        if output_path is None:
            output_path = str(server_dir / "manifest.json")
        prev_ops = set()
        try:
            prev_manifest = json.loads(Path(output_path).read_text(encoding='utf-8'))
            for gd in prev_manifest.get("groups", {}).values():
                prev_ops.update(gd.get("operations", {}).keys())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

        # Build complete manifest
        manifest = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "server_version": "3.0.0",
            "groups": {},
        }

        for group_name, group_info in registry.items():
            group_manifest = {
                "usage_file": group_info["usage_file"],
                "operation_count": len(group_info["operations"]),
                "operations": {},
            }

            for op_name, op_config in group_info["operations"].items():
                meta = tool_metadata.get(op_name, {})
                op_manifest = {
                    "script": op_config["script"],
                    "dispatch": op_config["dispatch"],
                    "timeout": op_config["timeout"],
                    "max_timeout": op_config["max_timeout"],
                    "has_preprocess": op_config["has_preprocess"],
                    "impl_func": meta.get("impl_func"),
                    "description": (meta.get("frontmatter", {}).get("description")
                                    or (meta.get("module_doc", "").split('\n')[0]
                                        if meta.get("module_doc") else "")),
                    "params": meta.get("params", []),
                    "lib_imports": meta.get("lib_imports", []),
                    "uses_canonicalize": meta.get("uses_canonicalize", False),
                    "uses_neo4j_driver": meta.get("uses_neo4j_driver", False),
                    "frontmatter": meta.get("frontmatter", {}),
                }
                group_manifest["operations"][op_name] = op_manifest

            manifest["groups"][group_name] = group_manifest

        manifest["total_operations"] = total

        # Write manifest file
        Path(output_path).write_text(
            json.dumps(manifest, indent=2, default=str),
            encoding='utf-8'
        )

        # Detect structural changes (new/removed operations)
        current_ops = set()
        for gd in manifest.get("groups", {}).values():
            current_ops.update(gd.get("operations", {}).keys())

        added_ops = current_ops - prev_ops
        removed_ops = prev_ops - current_ops
        structural_change = bool(added_ops or removed_ops)

        result = {
            "status": "manifest_generated",
            "path": output_path,
            "total_operations": total,
            "groups": {k: v["operation_count"] for k, v in manifest["groups"].items()},
        }

        # Auto-trigger sync_system_docs on structural changes
        if structural_change:
            result["structural_changes"] = {
                "added": sorted(added_ops) if added_ops else [],
                "removed": sorted(removed_ops) if removed_ops else [],
            }
            try:
                from tools.core.sync_system_docs import sync_system_docs_impl
                sync_result = sync_system_docs_impl(
                    sections=["landscape", "playbooks"],
                    driver=driver,
                )
                result["docs_synced"] = True
                result["sync_result"] = {
                    k: v.get("status") if isinstance(v, dict) else v
                    for k, v in sync_result.get("sections", {}).items()
                }
            except Exception as e:
                result["docs_synced"] = False
                result["sync_error"] = str(e)

        return result

    elif action == "report":
        # Human-readable summary
        lines = [f"# nicktools Registry Report ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
                 f"Total: {total} operations across {len(registry)} groups\n"]

        for group_name, group_info in registry.items():
            lines.append(f"## {group_name} ({len(group_info['operations'])} ops)")
            for op_name, op_config in group_info["operations"].items():
                meta = tool_metadata.get(op_name, {})
                dispatch = op_config["dispatch"]
                desc = (meta.get("module_doc", "").split('\n')[0][:80]
                        if meta.get("module_doc") else "no docstring")
                params = meta.get("params", [])
                required = [p["name"] for p in params if p.get("required")]
                libs = meta.get("lib_imports", [])

                line = f"  {op_name} [{dispatch}] -- {desc}"
                if required:
                    line += f"\n    required: {', '.join(required)}"
                if libs:
                    short_libs = [l.split('.')[-1] for l in libs]
                    line += f"\n    lib deps: {', '.join(short_libs)}"
                lines.append(line)
            lines.append("")

        if usage_issues:
            lines.append("## USAGE.md Issues")
            for issue in usage_issues:
                lines.append(f"  {issue['group']}: missing sections for {issue['missing_sections']}")

        if canonicalization_warnings:
            lines.append("\n## Canonicalization Warnings")
            for warn in canonicalization_warnings:
                lines.append(f"  {warn['operation']}: {warn['issue']}")

        return {"report": "\n".join(lines)}

    else:
        return {"error": f"Unknown action: {action}. Valid: validate, manifest, report"}


if __name__ == "__main__":
    from lib.io import setup_output, load_params, output
    setup_output()
    p = load_params()
    r = registry_sync_impl(**p)
    output(r)
