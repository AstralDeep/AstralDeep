from orchestrator.goal_auto_verify import _startup_argv


def test_startup_discovery_uses_declared_smoke_flag_without_project_specific_paths():
    root = "/workspace/game"
    main = root + "/main.py"

    assert _startup_argv(root, [main], {main: "if __name__ == '__main__': ... --smoke-test"}) == [
        "./.venv/bin/python", "main.py", "--smoke-test",
    ]


def test_startup_discovery_uses_conventional_python_entrypoint_when_no_smoke_flag():
    root = "/workspace/service"
    main = root + "/app.py"

    assert _startup_argv(root, [main], {main: "def main(): pass"}) == [
        "./.venv/bin/python", "app.py",
    ]


def test_package_entrypoint_uses_smoke_flag_from_delegated_module():
    root = "/workspace/game"
    entry = root + "/minecraft_clone/__main__.py"
    game = root + "/minecraft_clone/game.py"

    assert _startup_argv(root, [entry, game], {
        entry: "from .game import main\nraise SystemExit(main())",
        game: "def main(): handle('--smoke')",
    }) == ["./.venv/bin/python", "-m", "minecraft_clone", "--smoke"]
