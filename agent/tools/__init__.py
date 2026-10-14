from pathlib import Path
import importlib
import importlib.util
import json
import os
import sys


def _schema_issue(info):
    """Structural health of ``tool_info()["input_schema"]`` (R-a③, load layer).

    The registry layer cannot run this check: ``entry_reason`` is static (no
    import), while ``input_schema`` is a RETURN VALUE of ``tool_info()`` -- the
    same principle as the runtime identity gate (a static gate can never check
    a value without importing the module). An absent schema is legal: the P-b
    dispatch precheck skips by design. Returns ``(hard, advisory)``:

    - ``hard``: ``required`` is a well-formed string list but names keys that
      ``properties`` does not declare. Every call would be dispatch-rejected
      for the ghost name; passing it feeds an unexpected kwarg to the tool
      (silently swallowed by ``**kwargs`` or a TypeError) -- the tool is
      effectively bricked at call time. Fail-closed with a fixable report
      entry, mirroring the identity gate.
    - ``advisory``: malformations that merely degrade the P-b precheck, which
      already tolerates them (non-dict schema; ``required`` not a list of
      strings). The tool itself still works, so it LOADS; the issue rides the
      load report's ``schema_issues`` (record + feed the owner -- never a
      silent degradation, never an availability loss).
    """
    schema = info.get("input_schema") if isinstance(info, dict) else None
    if schema is None:
        return None, None
    if not isinstance(schema, dict):
        return None, (f"input_schema is {type(schema).__name__}, not dict "
                      f"(P-b precheck inactive)")
    req = schema.get("required")
    if req is None:
        return None, None
    if not isinstance(req, list) or any(not isinstance(k, str) for k in req):
        return None, ("input_schema.required is not a list of strings "
                      "(P-b precheck inactive)")
    props = schema.get("properties")
    if isinstance(props, dict):
        ghost = [k for k in req if k not in props]
        if ghost:
            return (f"required names not declared in properties: {ghost} "
                    f"(every call would be dispatch-rejected)", None)
    return None, None


def load_tools(logging=print, names=[], tools_dir=None, report_path=None):
    """Discover tool modules exposing ``tool_info()`` and ``tool_function``.

    Args:
        logging: logger callable.
        names: list of tool names, or 'all'/[] for none.
        tools_dir: optional directory to scan instead of ``agent/tools``. Used by
            the GAN roles to load their own operator / gating toolsets.
        report_path: optional JSON sink for the LOAD outcome (B27). Defaults to
            ``$GAN_TOOLS_LOAD_REPORT``. The assembly report only proves a file was
            COPIED into the toolset; a module that fails to import (or lost its
            tool API) is skipped right here with a log line nobody reads, so the
            framework could not tell "assembled" from "actually available". The
            report closes that gap and rides the existing toolset report into the
            node meta / role assembly report. Since the R-a③ schema gate the
            report also carries ``schema_issues`` (advisory entries for modules
            that load but whose malformed ``input_schema`` degrades the P-b
            precheck).
    """
    report_path = report_path or os.environ.get("GAN_TOOLS_LOAD_REPORT") or None
    base_dir = Path(__file__).parent
    tools_dir = Path(tools_dir) if tools_dir is not None else base_dir
    use_file_spec = tools_dir.resolve() != base_dir.resolve()

    tools = []
    loaded, skipped = [], []
    schema_issues = []

    # Get all Python files in the tools directory (excluding __init__.py)
    tool_files = sorted(f for f in tools_dir.glob("*.py") if f.stem != "__init__")

    if use_file_spec and str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    for tool_file in tool_files:
        # A malformed tool module must NOT break the whole toolset: log and skip.
        try:
            if use_file_spec:
                module_name = f"gan_tool_{tools_dir.name}_{tool_file.stem}"
                spec = importlib.util.spec_from_file_location(module_name, tool_file)
                if spec is None or spec.loader is None:
                    raise ImportError(f"Could not load tool spec: {tool_file}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            else:
                module_name = f"agent.tools.{tool_file.stem}"
                module = importlib.import_module(module_name)

            if not (hasattr(module, 'tool_info') and hasattr(module, 'tool_function')):
                logging(f"Skipping tool {tool_file}: missing tool_info/tool_function")
                skipped.append({"file": tool_file.name,
                                "reason": "missing tool_info/tool_function"})
                continue
            try:
                info = module.tool_info()
            except Exception as e:  # noqa: BLE001 -- one broken tool must not sink the toolset
                logging(f"Skipping tool {tool_file}: tool_info failed: {e}")
                skipped.append({"file": tool_file.name,
                                "reason": f"tool_info failed: {type(e).__name__}: {e}"[:200]})
                continue
            # n1 (runtime identity gate): the module file stem is the loader key
            # and the `names` filter, while `tool_info()["name"]` is what the
            # chat loop keys dispatch by. A mismatch would "assemble" a tool
            # that no call can ever reach (loaded in the report, dead in
            # dispatch). The shipped components are verified consistent; a
            # mismatch is therefore a contract bug -> load fail-closed with a
            # fixable report entry instead of silently dead-loading.
            if not isinstance(info, dict):
                logging(f"Skipping tool {tool_file}: "
                        f"tool_info returned {type(info).__name__}, not dict")
                skipped.append({"file": tool_file.name,
                                "reason": f"tool_info returned {type(info).__name__}, not dict"})
                continue
            info_name = str(info.get("name") or "")
            if info_name != tool_file.stem:
                logging(f"Skipping tool {tool_file}: tool_info name '{info_name}' "
                        f"!= file stem '{tool_file.stem}'")
                skipped.append({"file": tool_file.name,
                                "reason": f"name mismatch: tool_info name '{info_name}' "
                                          f"!= file stem '{tool_file.stem}' (runtime identity gate)"})
                continue
            # R-a③ (load-time schema gate): structural health of the schema
            # VALUE (see _schema_issue). A required/properties mismatch
            # fail-closes like the identity gate above; a precheck-degrading
            # malformation loads with a schema_issues advisory entry.
            hard, advisory = _schema_issue(info)
            if hard:
                logging(f"Skipping tool {tool_file}: input_schema mismatch: {hard}")
                skipped.append({"file": tool_file.name,
                                "reason": f"input_schema mismatch: {hard}"})
                continue
            if advisory:
                logging(f"Tool {tool_file}: {advisory}")
                schema_issues.append({"file": tool_file.name, "issue": advisory})
            tool_name = tool_file.stem
            if names and (names == 'all' or tool_name in names):
                tools.append({
                    'info': info,
                    'function': module.tool_function,
                    'name': tool_name,
                })
                loaded.append(tool_name)
            else:
                skipped.append({"file": tool_file.name,
                                "reason": "not selected by the design"})
        except Exception as e:
            logging(f"Skipping tool {tool_file}: import failed: {e}")
            skipped.append({"file": tool_file.name,
                            "reason": f"import failed: {type(e).__name__}: {e}"[:200]})
            continue

    if report_path:
        # best effort: the report must never break tool loading
        try:
            _dir = os.path.dirname(str(report_path))
            if _dir:
                os.makedirs(_dir, exist_ok=True)
            with open(str(report_path), "w", encoding="utf-8") as f:
                json.dump({"tools_dir": str(tools_dir), "names": names,
                           "loaded": loaded, "skipped": skipped,
                           "schema_issues": schema_issues},
                          f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001 -- advisory sink
            pass

    return tools
