"""Seals the pinned SRT runtime's private data mounts before executing the captured Bubblewrap binary.
The qualification runner copies this standalone adapter and its exact configuration into each private control directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

_MAX_BYTES = 1024 * 1024
_MAX_WORDS = 8192
_FLAGS = {
    "--new-session",
    "--die-with-parent",
    "--unshare-net",
    "--unshare-pid",
    "--unshare-user",
}
_SINGLE = {"--tmpfs", "--dev", "--proc", "--cap-drop"}
_DOUBLE = {"--bind", "--ro-bind", "--setenv"}
_PROBE = ["--unshare-user", "--dev-bind", "/", "/", "true"]


def _path(value, *, root=False):
    if (
        type(value) is not str
        or not value.startswith("/")
        or value.startswith("//")
        or "\0" in value
        or len(value) > 4096
        or os.path.normpath(value) != value
        or not root
        and value == "/"
    ):
        raise ValueError("invalid bound path")
    return value


def _expand(arguments):
    result = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--":
            result.extend(arguments[index:])
            break
        if word != "--args":
            count = (
                0
                if word in _FLAGS
                else 1
                if word in _SINGLE
                else 2
                if word in _DOUBLE
                else -1
            )
            if count < 0 or index + count >= len(arguments):
                raise ValueError("unknown or incomplete sandbox option")
            result.extend(arguments[index : index + count + 1])
            index += count + 1
            continue
        if index + 1 >= len(arguments):
            raise ValueError("missing arguments descriptor")
        value = arguments[index + 1]
        if (
            not value.isdecimal()
            or str(int(value)) != value
            or not 3 <= int(value) <= 1024
        ):
            raise ValueError("invalid arguments descriptor")
        descriptor = int(value)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("arguments descriptor is not a regular file")
            payload = os.pread(descriptor, _MAX_BYTES + 1, 0)
            if len(payload) > _MAX_BYTES or not payload.endswith(b"\0"):
                raise ValueError("invalid bounded arguments file")
            words = [word.decode("utf-8") for word in payload[:-1].split(b"\0")]
            if "--args" in words or "--" in words:
                raise ValueError("arguments file contains a command or descriptor")
            result.extend(words)
        finally:
            os.close(descriptor)
        index += 2
    return result


def _bounded(arguments):
    if (
        type(arguments) is not list
        or not 1 <= len(arguments) <= _MAX_WORDS
        or any(
            type(word) is not str or "\0" in word or len(word.encode("utf-8")) > 131071
            for word in arguments
        )
        or sum(len(word.encode("utf-8")) + 1 for word in arguments) > _MAX_BYTES
    ):
        raise ValueError("invalid bounded arguments")
    return arguments


def sealed_arguments(arguments, scratch):
    scratch = _path(scratch)
    original = _bounded(arguments)
    if original == _PROBE:
        return list(original)
    words = _bounded(_expand(original))
    flags = []
    mounts = []
    seals = []
    cap_drop = []
    index = 0
    while index < len(words):
        option = words[index]
        if option == "--":
            break
        count = (
            0
            if option in _FLAGS
            else 1
            if option in _SINGLE
            else 2
            if option in _DOUBLE
            else -1
        )
        if count < 0 or index + count >= len(words):
            raise ValueError("unknown or incomplete sandbox option")
        values = words[index + 1 : index + count + 1]
        if not count:
            flags.append(option)
        elif option == "--cap-drop":
            cap_drop.append(values[0])
        elif option != "--setenv":
            for value in values:
                _path(value, root=option in {"--bind", "--ro-bind"})
            mounts.append((option, *values))
            if option in {"--tmpfs", "--dev"} and values[-1] not in seals:
                seals.append(values[-1])
        index += count + 1
    boundary = index
    if boundary >= len(words) - 1:
        raise ValueError("effect command is absent")
    if (
        set(flags) != _FLAGS
        or len(flags) != len(_FLAGS)
        or cap_drop != ["ALL"]
        or ("--ro-bind", "/", "/") not in mounts
        or ("--bind", "/", "/") in mounts
        or ("--bind", scratch, scratch) not in mounts
        or mounts.count(("--dev", "/dev")) != 1
        or mounts.count(("--proc", "/proc")) != 1
        or any(
            m[0] == "--dev" and m[1] != "/dev" or m[0] == "--proc" and m[1] != "/proc"
            for m in mounts
        )
    ):
        raise ValueError("sandbox fences differ from the fixed profile")
    extra = [word for target in seals for word in ["--remount-ro", target]]
    return _bounded([*words[:boundary], *extra, *words[boundary:]])


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate configuration field")
        value[key] = item
    return value


def load_config(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o222
            or not 0 < info.st_size <= 16384
        ):
            raise ValueError("configuration custody is invalid")
        payload = stream.read(16385)
    if len(payload) != info.st_size:
        raise ValueError("configuration changed during capture")
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=_object)
    if type(value) is not dict or set(value) != {"binary", "scratch"}:
        raise ValueError("invalid closed configuration")
    return _path(value["binary"]), _path(value["scratch"])


def run(arguments, config_path):
    try:
        binary, scratch = load_config(config_path)
        sealed = sealed_arguments(arguments, scratch)
        os.execv(binary, [binary, *sealed])
        raise OSError("sandbox executable returned")
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError):
        print("sandbox_bwrap_unavailable", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:], Path(__file__).with_suffix(".json")))
