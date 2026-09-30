"""plainsolid: minimal parametric CAD over a Python model file.

Model files do `from plainsolid import *` and get only the DSL names.
Library code imports submodules explicitly.
"""

import os as _os


def _import_the_kernel_quietly() -> None:
    """build123d loads the fontconfig that ships with OCCT's wheel, older than many systems'
    (Arch's 2.18): it cannot parse their newer font configuration and prints dozens of lines
    about it on stderr, while fonts work regardless. Silence stderr for that one first import;
    an import that fails still raises."""
    try:
        saved = _os.dup(2)
    except OSError:  # no stderr to silence
        import build123d
        return
    null = _os.open(_os.devnull, _os.O_WRONLY)
    try:
        _os.dup2(null, 2)
        import build123d  # noqa: F401
    finally:
        _os.dup2(saved, 2)
        _os.close(saved)
        _os.close(null)


_import_the_kernel_quietly()

from .dsl import (
    BACK,
    BOTTOM,
    FRONT,
    ISO,
    LEFT,
    RIGHT,
    TOP,
    XY,
    XZ,
    YZ,
    X,
    Y,
    Z,
    angle,
    chamfer,
    circular_pattern,
    coincident,
    concentric,
    cut,
    dimension,
    distance,
    extrude,
    fillet,
    fixed,
    import_step,
    instance,
    linear_pattern,
    meta,
    mirror,
    note,
    parallel,
    param,
    plane,
    revolve,
    shell,
    sketch,
    view,
)

__version__ = "0.0.1"
__all__ = [
    "BACK",
    "BOTTOM",
    "FRONT",
    "ISO",
    "LEFT",
    "RIGHT",
    "TOP",
    "XY",
    "XZ",
    "YZ",
    "X",
    "Y",
    "Z",
    "angle",
    "chamfer",
    "circular_pattern",
    "coincident",
    "concentric",
    "cut",
    "dimension",
    "distance",
    "extrude",
    "fillet",
    "fixed",
    "import_step",
    "instance",
    "linear_pattern",
    "meta",
    "mirror",
    "note",
    "parallel",
    "param",
    "plane",
    "revolve",
    "shell",
    "sketch",
    "view",
]
