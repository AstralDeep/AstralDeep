# Confines one tool call before it runs: Landlock lets it read the system and the agent's data and write only its own
# scratch directory, with no /proc, socket files or signals outside its tree; seccomp blocks IPC and networking.
# Run as `python confine.py --scratch DIR --read PATH ... -- argv...`; it applies the rules, then execs.

from __future__ import annotations

import ctypes
import os
import platform
import resource
import struct
import sys

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
REQUIRED_ABI = 6  # filesystem, TCP and signal/abstract-socket scoping

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1, 2, 4, 8
MAKE_SOCK = 1 << 9  # never granted: Landlock does not mediate connect() to a socket file, so none may be made
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | (1 << 14)  # rights that apply to a single file (with truncate)
READ_RIGHTS = EXECUTE | READ_FILE | READ_DIR
NET_BIND_TCP, NET_CONNECT_TCP = 1, 2
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1, 2
SYSTEM_PATHS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt")
DEVICES = ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random")

_libc = ctypes.CDLL(None, use_errno=True)


def abi_version() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    version = _libc.syscall(SYS_CREATE_RULESET, None, 0, CREATE_RULESET_VERSION)
    return max(version, 0)


def _fs_rights(abi: int) -> int:
    # Every filesystem right this kernel knows; anything handled and not granted below is denied.
    known = {1: 13, 2: 14, 3: 15, 4: 15, 5: 16}
    return (1 << known.get(abi, 16)) - 1


def apply(scratch: str, readable: list[str]) -> None:
    abi = abi_version()
    if abi < REQUIRED_ABI:
        raise OSError(f"Landlock ABI {REQUIRED_ABI} or newer is required; this kernel offers {abi}")
    fs = _fs_rights(abi)
    attr = struct.pack("QQQ", fs, NET_BIND_TCP | NET_CONNECT_TCP, SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL)
    ruleset = _libc.syscall(SYS_CREATE_RULESET, attr, len(attr), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    try:
        grants = [(p, READ_RIGHTS) for p in (*SYSTEM_PATHS, *readable)]
        grants += [(d, READ_FILE | WRITE_FILE) for d in DEVICES] + [(scratch, fs & ~MAKE_SOCK)]
        for path, rights in grants:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # a path this image lacks needs no rule
            try:
                if not os.path.isdir(path):
                    rights &= FILE_RIGHTS
                rule = struct.pack("=Qi", rights & fs, fd)
                if _libc.syscall(SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH, rule, 0) != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
            finally:
                os.close(fd)
        if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS) failed")
        if _libc.syscall(SYS_RESTRICT_SELF, ruleset, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset)
    block_shared_ipc()


# SysV IPC and POSIX message queues are shared by everything running as one user, and Landlock covers neither them
# nor UDP, so a call could hand data to another call through them. Calls have no network, so internet sockets go too.
# setsid and setpgid go as well: every process a call starts stays in its session, so ending the call ends them all.
# Each entry: audit arch, the socket syscall, and the blocked syscalls.
SYSCALLS = {
    "x86_64": (0xC000003E, 41, (29, 30, 31, 64, 65, 66, 67, 68, 69, 70, 71, 220, 240, 241, 242, 243, 244, 245,
                                109, 112)),
    "aarch64": (0xC00000B7, 198, (180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190, 191, 192, 193, 194, 195,
                                  196, 197, 154, 157)),
}
MAX_OPEN_FILES = 4096  # keeps the server's scan of a call's open files cheap
AF_INET, AF_INET6 = 2, 10
PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 22, 2
RET_ALLOW, RET_EPERM = 0x7FFF0000, 0x00050000 | 1
LD_ABS, JEQ, JGE, RET = 0x20, 0x15, 0x35, 0x06


def block_shared_ipc() -> None:
    machine = platform.machine()
    if machine not in SYSCALLS:
        raise OSError(f"no syscall filter for {machine}")
    arch, socket_nr, ipc = SYSCALLS[machine]
    # (code, k, jump-if-true target, jump-if-false target); targets are labels resolved below.
    prog: list[tuple[int, int, str | None, str | None]] = [(LD_ABS, 4, None, None), (JEQ, arch, None, "deny"),
                                                          (LD_ABS, 0, None, None)]
    if machine == "x86_64":
        prog.append((JGE, 0x40000000, "deny", None))  # x32 syscall numbers would slip past the list
    prog += [(JEQ, nr, "deny", None) for nr in ipc]
    prog += [(JEQ, socket_nr, None, "allow"), (LD_ABS, 16, None, None),  # the socket's address family
             (JEQ, AF_INET, "deny", None), (JEQ, AF_INET6, "deny", None)]
    labels = {"allow": len(prog), "deny": len(prog) + 1}
    code = b""
    for i, (op, k, jt, jf) in enumerate(prog):
        here = i + 1
        code += struct.pack("HBBI", op, labels[jt] - here if jt else 0, labels[jf] - here if jf else 0, k)
    code += struct.pack("HBBI", RET, 0, 0, RET_ALLOW) + struct.pack("HBBI", RET, 0, 0, RET_EPERM)
    buffer = ctypes.create_string_buffer(code, len(code))
    fprog = struct.pack("HxxxxxxP", len(code) // 8, ctypes.addressof(buffer))
    if _libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.c_char_p(fprog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "installing the syscall filter failed")


def main(argv: list[str]) -> None:
    scratch, readable, fsize = "", [], 0
    while argv and argv[0] != "--":
        flag, value, argv = argv[0], argv[1], argv[2:]
        if flag == "--scratch":
            scratch = value
        elif flag == "--read":
            readable.append(value)
        elif flag == "--fsize":
            fsize = int(value)
        else:
            raise SystemExit(f"unknown flag {flag}")
    command = argv[1:]
    if not scratch or not command:
        raise SystemExit("usage: confine --scratch DIR [--read PATH]... -- argv...")
    try:
        if fsize:
            # No single file the call writes may outgrow the turn's disk limit.
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        cap = MAX_OPEN_FILES if hard == resource.RLIM_INFINITY else min(MAX_OPEN_FILES, hard)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(soft, cap), cap))
        apply(scratch, readable)
    except OSError:
        sys.stderr.write("the sandbox could not confine this call\n")
        os._exit(126)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
