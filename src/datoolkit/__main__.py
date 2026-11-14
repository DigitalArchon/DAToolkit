"""`python -m datoolkit`: how the AppImage starts the app (see packaging/appimage).
`python -m datoolkit replay ...` (or `DAToolkit.AppImage replay ...`) runs datoolkit-replay."""

from . import coredumps

coredumps.no_core_dumps()          # before anything else is loaded (coredumps.py)

import sys  # noqa: E402

if sys.argv[1:2] == ["replay"]:
    from .replay import main as replay

    sys.exit(replay(sys.argv[2:]))
else:
    from .app import main

    main()
