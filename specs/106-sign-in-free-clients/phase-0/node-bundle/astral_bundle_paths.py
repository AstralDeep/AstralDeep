"""Points the bundled Python at the native tree beside it, so libmagic, poppler, ripgrep and fontconfig resolve without DYLD variables.
build_bundle.sh installs it into site-packages with a .pth file, and python-magic and pdf2image then find their libraries and tools inside the bundle.
"""

import ctypes.util
import os
import sys

_NATIVE = os.path.normpath(os.path.join(sys.prefix, os.pardir, "native"))
_original_find_library = ctypes.util.find_library


def _find_library(name):
    candidate = os.path.join(_NATIVE, "lib", f"lib{name}.1.dylib")
    if os.path.isfile(candidate):
        return candidate
    return _original_find_library(name)


if os.path.isdir(_NATIVE):
    ctypes.util.find_library = _find_library
    os.environ["PATH"] = os.pathsep.join((os.path.join(_NATIVE, "bin"), os.environ.get("PATH", "")))
    os.environ.setdefault("MAGIC", os.path.join(_NATIVE, "share", "misc", "magic.mgc"))
    os.environ.setdefault("FONTCONFIG_FILE", os.path.join(_NATIVE, "etc", "fonts", "fonts.conf"))
