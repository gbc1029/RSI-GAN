from pathlib import Path
import importlib
import importlib.util
import sys


def load_tools(logging=print, names=[], tools_dir=None):
    """Discover tool modules exposing ``tool_info()`` and ``tool_function``.

    Args:
        logging: logger callable.
        names: list of tool names, or 'all'/[] for none.
        tools_dir: optional directory to scan instead of ``agent/tools``. Used by
            the GAN roles to load their own operator / gating toolsets.
    """
    base_dir = Path(__file__).parent
    tools_dir = Path(tools_dir) if tools_dir is not None else base_dir
    use_file_spec = tools_dir.resolve() != base_dir.resolve()

    tools = []

    # Get all Python files in the tools directory (excluding __init__.py)
    tool_files = sorted(f for f in tools_dir.glob("*.py") if f.stem != "__init__")

    if use_file_spec and str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    for tool_file in tool_files:
        # Import the module
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

            # Check if module has required functions
            if hasattr(module, 'tool_info') and hasattr(module, 'tool_function'):
                tool_name = tool_file.stem
                if names and (names == 'all' or tool_name in names):
                    tools.append({
                        'info': module.tool_info(),
                        'function': module.tool_function,
                        'name': tool_name,
                    })
            else:
                raise Exception(f"Tool module {module_name} does not have required functions.")
        except Exception as e:
            # Log the error and raise it
            logging(f"Failed to import {tool_file}: {e}")
            raise e

    return tools
