"""ShellShifter public entry point; legacy modules share the same scene state."""
import importlib
import uv_straight as _tool


def __getattr__(name):
    return getattr(_tool, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_tool)))


def show():
    return _tool.show()


def reload_tool():
    """Reload the implementation and display backend in an existing session."""
    global _tool
    _tool = importlib.reload(_tool)
    return _tool.reload_tool()


def install_menu():
    import uv_straight_menu
    from install_shellshifter import rename_existing_buttons
    uv_straight_menu.install()
    rename_existing_buttons()


def uninstall_menu():
    import uv_straight_menu
    uv_straight_menu.uninstall()


if __name__ == '__main__':
    show()
