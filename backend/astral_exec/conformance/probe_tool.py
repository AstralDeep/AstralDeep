"""The untrusted tool the conformance suite runs inside the sandbox, one mode per launch: it attempts operations and prints one JSON line per check.
Only an outcome (ok, an errno name or an exception name) is reported, and file probes open paths without printing their content.
"""

import errno
import json
import os
import sys

CONFIG = json.loads(sys.argv[1])
WORKSPACE = CONFIG["workspace"]
STEP = 8 * 1024 * 1024
ADDED_BY_RUNTIME = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING", "ASTRAL_EXEC_MARKER"}


def emit(check, value):
    print(json.dumps({"check": check, "outcome": value}), flush=True)


def outcome(action):
    import socket

    try:
        detail = action()
    except socket.gaierror:
        return "gaierror"
    except OSError as error:
        if error.errno:
            return errno.errorcode.get(error.errno, f"errno{error.errno}")
        return type(error).__name__
    except Exception as error:
        return type(error).__name__
    return detail or "ok"


def report(check, action):
    emit(check, outcome(action))


def write_file(path):
    def run():
        with open(path, "w") as handle:
            handle.write("conformance")

    return run


def open_only(path, flags=os.O_RDONLY):
    def run():
        os.close(os.open(path, flags))

    return run


def tcp(host, port):
    import socket

    def run():
        with socket.create_connection((host, port), timeout=1.5):
            pass

    return run


def udp(host, port):
    import socket

    query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"

    def run():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.5)
            sock.sendto(query, (host, port))
            sock.recvfrom(512)

    return run


def unix(path):
    import socket

    def run():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.5)
            sock.connect(path)

    return run


def listen(host):
    import socket

    def run():
        with socket.socket() as sock:
            sock.bind((host, 0))
            sock.listen(1)

    return run


def symlink_write():
    link = os.path.join(WORKSPACE, "link")
    os.symlink(CONFIG["outside"], link)
    write_file(os.path.join(link, "via-link.txt"))()


def hardlink():
    os.link(
        os.path.join(CONFIG["outside"], "secret.txt"), os.path.join(WORKSPACE, "hard")
    )


def rename_out():
    source = os.path.join(WORKSPACE, "moving.txt")
    write_file(source)()
    os.rename(source, os.path.join(CONFIG["outside"], "moved.txt"))


def load_library(path):
    def run():
        import ctypes

        ctypes.CDLL(path)

    return run


def map_files():
    report("map_original", load_library(CONFIG["library"]))
    report("map_copy", load_library(os.path.join(WORKSPACE, "copied.so")))


def fork_once():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)


def spawn_echo():
    import subprocess

    result = subprocess.run(["/bin/echo", "child"], capture_output=True, timeout=5)
    return "ok" if result.returncode == 0 else f"exit{result.returncode}"


def thread_runs():
    import threading

    seen = []
    thread = threading.Thread(target=lambda: seen.append(True))
    thread.start()
    thread.join()
    return "ok" if seen else "no"


def kill_outsider():
    os.kill(CONFIG["outsider"], 0)


def mach_lookup(name):
    import ctypes

    libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    libsystem.bootstrap_look_up.argtypes = [
        ctypes.c_uint,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint),
    ]
    libsystem.bootstrap_look_up.restype = ctypes.c_int
    bootstrap = ctypes.c_uint.in_dll(libsystem, "bootstrap_port")
    port = ctypes.c_uint()
    status = libsystem.bootstrap_look_up(
        bootstrap.value, name.encode(), ctypes.byref(port)
    )
    return "ok" if status == 0 else f"kr{status}"


def mach():
    for name in CONFIG["services"]:
        emit("mach:" + name, mach_lookup(name))


def socket_pair(domain, kind):
    import socket

    def run():
        socket.socket(getattr(socket, domain), getattr(socket, kind)).close()

    return run


def socket_pair_open():
    import socket

    for end in socket.socketpair():
        end.close()


def io_uring():
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(120)
    if libc.syscall(425, 4, buffer) >= 0:
        return "ok"
    raise OSError(ctypes.get_errno(), "io_uring_setup")


def base():
    outside, readable, home = CONFIG["outside"], CONFIG["readable"], CONFIG["home"]
    report("write_workspace", write_file(os.path.join(WORKSPACE, "inside.txt")))
    report("read_workspace", open_only(os.path.join(WORKSPACE, "inside.txt")))
    report("write_outside", write_file(os.path.join(outside, "escape.txt")))
    report(
        "write_outside_existing",
        open_only(os.path.join(outside, "secret.txt"), os.O_WRONLY),
    )
    report("write_read_root", write_file(os.path.join(readable, "new.txt")))
    report(
        "write_home_startup",
        open_only(os.path.join(home, ".zshrc"), os.O_WRONLY | os.O_CREAT),
    )
    report("write_system", write_file("/usr/astral-conformance.txt"))
    report("write_tmp", write_file("/tmp/astral-conformance.txt"))
    report("write_via_symlink", symlink_write)
    report("hardlink_outside", hardlink)
    report("rename_outside", rename_out)
    report("read_read_root", open_only(os.path.join(readable, "readable.txt")))
    report("read_outside", open_only(os.path.join(outside, "secret.txt")))
    report("read_data_dir", open_only(os.path.join(CONFIG["data"], "node.db")))
    report("read_home_ssh", open_only(os.path.join(home, ".ssh", "id_ed25519")))
    report("read_home_dotfile", open_only(os.path.join(home, ".zshrc")))
    report("list_outside", lambda: len(os.listdir(outside)) >= 0 and "ok")
    if sys.platform == "darwin":
        report("read_system_keychain", open_only("/Library/Keychains/System.keychain"))
        report(
            "read_framework_keychain",
            open_only("/System/Library/Keychains/SystemRootCertificates.keychain"),
        )
        report("unix_mdnsresponder", unix("/private/var/run/mDNSResponder"))
    report("tcp_public", tcp("192.0.2.1", 443))
    report("tcp_node_port", tcp("127.0.0.1", CONFIG["tcp_port"]))
    report("udp_loopback", udp("127.0.0.1", CONFIG["udp_port"]))
    report("udp_public", udp("192.0.2.53", 53))
    report("unix_fixture", unix(CONFIG["unix_path"]))
    report("listen_loopback", listen("127.0.0.1"))
    report("listen_all", listen("0.0.0.0"))
    report("fork", fork_once)
    report("spawn", spawn_echo)
    report("thread", thread_runs)
    report("kill_outsider", kill_outsider)
    emit("environ", sorted(os.environ))
    emit(
        "leaked",
        sorted(
            k
            for k in CONFIG["parent_keys"]
            if k in os.environ
            and k not in CONFIG["passed"]
            and k not in ADDED_BY_RUNTIME
        ),
    )
    import resource

    emit("rlimit_core", list(resource.getrlimit(resource.RLIMIT_CORE)))
    emit("rlimit_nofile", list(resource.getrlimit(resource.RLIMIT_NOFILE)))
    emit("rlimit_fsize", list(resource.getrlimit(resource.RLIMIT_FSIZE)))
    if sys.platform == "linux":
        emit("rlimit_as", list(resource.getrlimit(resource.RLIMIT_AS)))
        for name, domain, kind in (
            ("socket_inet_stream", "AF_INET", "SOCK_STREAM"),
            ("socket_inet6_stream", "AF_INET6", "SOCK_STREAM"),
            ("socket_inet_dgram", "AF_INET", "SOCK_DGRAM"),
            ("socket_inet_raw", "AF_INET", "SOCK_RAW"),
            ("socket_unix", "AF_UNIX", "SOCK_STREAM"),
            ("socket_netlink", "AF_NETLINK", "SOCK_RAW"),
            ("socket_packet", "AF_PACKET", "SOCK_RAW"),
        ):
            report(name, socket_pair(domain, kind))
        report("io_uring", io_uring)
        report("socketpair", socket_pair_open)


def keychain():
    home = CONFIG["home"]
    report("read_home_dotfile", open_only(os.path.join(home, ".zshrc")))
    report(
        "read_home_keychain",
        open_only(os.path.join(home, "Library", "Keychains", "login.keychain-db")),
    )


def tunnel_through(target, proxy):
    import socket

    with socket.create_connection(proxy, timeout=5) as sock:
        sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        status = int(head.split(b" ")[1]) if head.startswith(b"HTTP/") else 0
        body = ""
        if status == 200:
            sock.sendall(b"ping")
            body = sock.recv(4096).decode()
        return [status, body]


def https_through(proxy, cert):
    import socket
    import ssl

    context = ssl.create_default_context(cafile=cert)
    with socket.create_connection(proxy, timeout=5) as sock:
        sock.sendall(
            b"CONNECT fixture.example.com:443 HTTP/1.1\r\nHost: fixture.example.com:443\r\n\r\n"
        )
        head = sock.recv(4096)
        if b" 200 " not in head.split(b"\r\n", 1)[0]:
            return "proxy:" + head.split(b"\r\n", 1)[0].decode()
        with context.wrap_socket(sock, server_hostname="fixture.example.com") as tls:
            tls.sendall(b"GET / HTTP/1.1\r\nHost: fixture.example.com\r\n\r\n")
            return "ok:" + tls.recv(4096).split(b"\r\n\r\n", 1)[-1].decode()


def https_by_environment(cert):
    import ssl
    import urllib.request

    context = ssl.create_default_context(cafile=cert)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(), urllib.request.HTTPSHandler(context=context)
    )
    return (
        "ok:" + opener.open("https://fixture.example.com/", timeout=10).read().decode()
    )


def _tunnel_outcome(target, proxy):
    try:
        return tunnel_through(target, proxy)
    except Exception as error:
        return type(error).__name__


def hosted():
    import urllib.parse

    proxy_url = urllib.parse.urlsplit(os.environ["HTTPS_PROXY"])
    proxy = (proxy_url.hostname, proxy_url.port)
    emit("proxy_env", os.environ["HTTPS_PROXY"])
    for target in CONFIG["targets"]:
        emit("connect:" + target, _tunnel_outcome(target, proxy))
    report("https_via_proxy", lambda: https_through(proxy, CONFIG["cert"]))
    report("https_by_environment", lambda: https_by_environment(CONFIG["cert"]))
    report("tcp_node_port", tcp("127.0.0.1", CONFIG["tcp_port"]))
    report("tcp_echo_direct", tcp("127.0.0.1", CONFIG["echo_port"]))
    report("tcp_other_proxy", tcp("127.0.0.1", CONFIG["other_proxy_port"]))
    report("udp_loopback", udp("127.0.0.1", CONFIG["udp_port"]))
    report("unix_fixture", unix(CONFIG["unix_path"]))
    if sys.platform == "linux":
        for name, domain, kind in (
            ("socket_inet_stream", "AF_INET", "SOCK_STREAM"),
            ("socket_inet6_stream", "AF_INET6", "SOCK_STREAM"),
            ("socket_inet_dgram", "AF_INET", "SOCK_DGRAM"),
            ("socket_inet_raw", "AF_INET", "SOCK_RAW"),
            ("socket_unix", "AF_UNIX", "SOCK_STREAM"),
            ("socket_netlink", "AF_NETLINK", "SOCK_RAW"),
        ):
            report(name, socket_pair(domain, kind))


def exec_script():
    path = os.path.join(WORKSPACE, "run.sh")
    with open(path, "w") as handle:
        handle.write("#!/bin/sh\necho EXEC-RAN\n")
    os.chmod(path, 0o755)
    try:
        os.execv(path, [path])
    except OSError as error:
        emit("exec_script", errno.errorcode.get(error.errno, "OSError"))


def target_binary(mode):
    import shutil

    if not CONFIG["copy"]:
        return CONFIG["python"]
    path = os.path.join(WORKSPACE, "copied")
    shutil.copyfile(CONFIG["python"], path)
    os.chmod(path, mode)
    return path


def exec_binary():
    path = target_binary(0o755)
    try:
        os.execv(path, [path, "-I", "-S", "-c", "print('EXEC-RAN')"])
    except OSError as error:
        emit("exec_binary", errno.errorcode.get(error.errno, "OSError"))


def exec_loader():
    path = target_binary(0o644)
    loader = CONFIG["loader"]
    try:
        os.execv(loader, [loader, path, "-I", "-S", "-c", "print('EXEC-RAN')"])
    except OSError as error:
        emit("exec_loader", errno.errorcode.get(error.errno, "OSError"))


def wait_for_release():
    with open(os.path.join(WORKSPACE, "started"), "w") as handle:
        handle.write("1")
    import time

    deadline = time.monotonic() + 60
    release = os.path.join(WORKSPACE, "release")
    while not os.path.exists(release) and time.monotonic() < deadline:
        time.sleep(0.02)


def sleepers():
    import subprocess

    token = CONFIG["token"]
    spawned = 0
    for index in range(CONFIG["count"]):
        detached = bool(index % 2)
        argv = ["/bin/sleep", f"{token}{index}"]
        if detached:
            argv = [
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import time; time.sleep(600)",
                f"{token}{index}",
            ]
        try:
            subprocess.Popen(argv, start_new_session=detached)
            spawned += 1
        except OSError:
            pass
    emit("spawned", spawned)
    if CONFIG["linger"]:
        import time

        time.sleep(CONFIG["linger"])


def time_limit():
    import signal
    import time

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    emit("started", "ok")
    while True:
        time.sleep(0.1)


def memory():
    import time

    emit("ready", "ok")
    blocks, total = [], 0
    try:
        while total < CONFIG["ceiling"]:
            block = bytearray(STEP)
            block[::4096] = b"\x01" * (STEP // 4096)
            blocks.append(block)
            total += STEP
            emit("allocated", total)
            time.sleep(0.01)
    except MemoryError:
        emit("stopped", "MemoryError")
        return
    emit("stopped", "ceiling")


def file_size():
    import signal

    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    chunk = b"x" * 65536
    emit("started", "ok")
    with open(os.path.join(WORKSPACE, "big.bin"), "wb") as handle:
        for _ in range(CONFIG["chunks"]):
            handle.write(chunk)
            handle.flush()
    emit("stopped", "ceiling")


def open_files():
    held = []
    refused = None
    for _ in range(CONFIG["attempts"]):
        try:
            held.append(open(CONFIG["readable_file"], "rb"))
        except OSError as error:
            refused = errno.errorcode.get(error.errno, "OSError")
            break
    emit("opened", len(held))
    emit("refused", refused)


def marker():
    with open(os.path.join(WORKSPACE, "ran"), "w") as handle:
        handle.write("1")


MODES = {
    "base": base,
    "keychain": keychain,
    "map": map_files,
    "mach": mach,
    "hosted": hosted,
    "exec_script": exec_script,
    "exec_binary": exec_binary,
    "exec_loader": exec_loader,
    "wait": wait_for_release,
    "sleepers": sleepers,
    "time": time_limit,
    "memory": memory,
    "file_size": file_size,
    "open_files": open_files,
    "marker": marker,
}

MODES[CONFIG["mode"]]()
