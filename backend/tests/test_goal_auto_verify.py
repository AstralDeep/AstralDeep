from orchestrator.goal_auto_verify import (
    _javascript_http_startup_argv,
    _javascript_package_manager,
    _javascript_script_argv,
    _startup_argv,
)


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


def test_javascript_package_manager_prefers_locked_install_and_supports_unlocked_npm():
    assert _javascript_package_manager({"package-lock.json"}) == (
        "npm", ["npm", "ci", "--no-audit", "--no-fund"],
    )
    assert _javascript_package_manager({"pnpm-lock.yaml"}) == (
        "pnpm", ["pnpm", "install", "--frozen-lockfile"],
    )
    assert _javascript_package_manager({"yarn.lock"}) == (
        "yarn", ["yarn", "install", "--immutable"],
    )
    assert _javascript_package_manager({"bun.lock"})[0] == "bun"
    assert _javascript_package_manager({"package.json"}) == (
        "npm", ["npm", "install", "--no-audit", "--no-fund"],
    )


def test_javascript_test_command_and_http_startup_are_bounded_and_local():
    assert _javascript_script_argv("npm", "test") == ["npm", "run", "test"]
    argv = _javascript_http_startup_argv("npm", {"preview": "vite preview"})
    assert argv[:2] == ["node", "-e"]
    assert "127.0.0.1:4173" in argv[2]
    assert "20000" in argv[2]
    assert "SIGTERM" in argv[2]
    assert _javascript_http_startup_argv("npm", {"test": "vitest"}) is None
