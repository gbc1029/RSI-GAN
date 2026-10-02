from pathlib import Path
import importlib
import importlib.util
import json
import os
import sys


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
            node meta / role assembly report.
    """
    report_path = report_path or os.environ.get("GAN_TOOLS_LOAD_REPORT") or None
    base_dir = Path(__file__).parent
    tools_dir = Path(tools_dir) if tools_dir is not None else base_dir
    use_file_spec = tools_dir.resolve() != base_dir.resolve()

    tools = []
    loaded, skipped = [], []

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
                           "loaded": loaded, "skipped": skipped},
                          f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001 -- advisory sink
            pass

    return tools
