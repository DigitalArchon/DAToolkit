"""`python -m datoolkit`: how the AppImage starts the app (see packaging/appimage).
`python -m datoolkit replay ...` (or `DAToolkit.AppImage replay ...`) runs datoolkit-replay."""

import sys

if sys.argv[1:2] == ["replay"]:
    from .replay import main as replay

    sys.exit(replay(sys.argv[2:]))
else:
    from .app import main

    main()
