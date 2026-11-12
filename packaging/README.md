# Packaging: the AppImage

DA Toolkit ships as a single AppImage for x86-64 Linux, built so that anyone can rebuild a
release from its commit and get the same file, byte for byte.

## What's in it, and what comes from the system

| In the AppImage | From the system |
|---|---|
| Python 3.12 ([python-build-standalone](https://github.com/astral-sh/python-build-standalone)) | GTK 3 and **WebKitGTK 4.1** (the app window) |
| Every Python dependency, pinned with hashes | `ssh`, `setsid` (util-linux), `xdg-open` |
| PyGObject and pycairo, built from pinned sources | `guacd` for RDP (optional) |
| gssapi and krb5 (WinRM Kerberos), built from pinned sources | MIT Kerberos' `libgssapi_krb5` (installed wherever OpenSSH is) |
| A fallback `libgirepository-2.0` for hosts that lack it | The Secret Service keyring (GNOME Keyring, KWallet) |

WebKitGTK is deliberately **not** bundled: it renders the AI's output and the RDP canvas, so it
should get the distribution's security updates, not wait for a DA Toolkit release. Without it,
DA Toolkit opens in the default web browser instead and says what to install:

| Distribution | Install the app window's engine |
|---|---|
| Ubuntu 24.04+, Mint 22+, Debian 13+ | `sudo apt install gir1.2-webkit2-4.1` |
| Fedora | `sudo dnf install webkit2gtk4.1` |
| Arch, CachyOS | `sudo pacman -S webkit2gtk-4.1` |

The bundled PyGObject needs GLib 2.80 or newer, which sets the oldest supported systems:
Ubuntu 24.04 / Mint 22, Debian 13, and current Fedora and Arch. The AppImage uses the static
type 2 runtime, so it doesn't need `libfuse2`.

## Building

The machine that builds needs four things, and `build-appimage.sh` says which is missing before
it starts: rootless [Podman](https://podman.io) (`sudo apt install podman`), git, GNU tar (its
`--sort=name` packs the commit's files in one order) and python3 3.11 or newer (its `tomllib`
reads the version from `pyproject.toml`). Everything else the build uses is in the container.

```bash
packaging/build-appimage.sh build    # dist/DAToolkit-<version>-x86_64.AppImage (+ .sha256)
packaging/build-appimage.sh check    # build twice from scratch, compare: must be identical
packaging/test-appimage.sh           # smoke-test it on Ubuntu, Debian, Fedora, Arch, CachyOS
packaging/test-locked.sh             # the test suite, on the AppImage's own Python and packages
packaging/build-appimage.sh lock     # lock what was added to the requirements (review the diff, commit)
UPGRADE=1 packaging/build-appimage.sh lock   # re-resolve every lock to today's newest
DIST=/elsewhere packaging/build-appimage.sh build   # the results somewhere other than dist/
```

Only committed files are built: HEAD's, read from git's own objects exactly as committed, so no
`core.autocrlf`, filter or `tar.umask` of the machine's changes them (as `git archive` would),
and every timestamp is HEAD's commit time (`SOURCE_DATE_EPOCH`). What builds them is the
commit's too. `build.sh`, the locks and the pins come out of the commit with the sources;
`build-appimage.sh` itself is the file that was run, and it reads `pins.sh` and the
Containerfile from the working tree to make the builder image, so it first compares itself and
everything in `packaging/appimage/` with HEAD's, byte for byte, and builds nothing while any of
them differs: it names the files, to be committed or put aside. Other uncommitted changes are
not built, and a note says so. The lock run reads
`pyproject.toml` and the `*.in` files as they are in the working tree, so a requirement is
locked before it is committed, and keeps every pin the current locks hold but what the change
moves. Builds run in `packaging/appimage/Containerfile`: Ubuntu 24.04 pinned
by digest, with packages from the Ubuntu archive as it was at a fixed moment
([snapshot.ubuntu.com](https://snapshot.ubuntu.com)). The host's CA bundle goes into the
builder image's build as a copy (looked for where AppRun looks: Debian's, Fedora's, openSUSE's
and the rest), for apt to reach the snapshot over https, and is removed again; it is in no byte
of the AppImage. The builder image is made on the machine and run from there (`--pull=never`:
no registry is asked for it). Downloads are https only, redirects included, cached in
`~/.cache/datoolkit-appimage` and verified on every build.

## What is pinned, and where

| What | Pinned in |
|---|---|
| Base image (digest), apt snapshot, Python build, AppImage runtime | `appimage/pins.sh` |
| Runtime Python packages (wheels, with hashes) | `appimage/requirements.lock` |
| PyGObject, pycairo (source archives, with hashes) | `appimage/sdists.lock` |
| meson, meson-python, ninja, setuptools, Cython (with hashes) | `appimage/build-requirements.lock` |
| pip and pip-tools, which write the locks, and what they run on (with hashes) | `appimage/lock-requirements.lock` |
| pytest and pytest-asyncio, for `test-locked.sh` (with hashes; not in the AppImage) | `appimage/test-requirements.lock` |

Changing any of these but the last changes the AppImage, which is the point: an update is a
reviewed commit.
Nothing of the build machine gets in: bytecode is unchecked-hash and names its source as it is
inside the image (`/usr/python/lib/...`), pip's console scripts (which would name the build's
Python) are left out, and the build stops if any file still names the folder it was built in.
Each package's `RECORD` is then made to say what is in the image (`appimage/records.py`): the
lines of files left out are dropped (the console scripts, Pillow's font module, and
`direct_url.json`, whose line carried a hash that followed from the bytes of a wheel archive
made during the build), and an extension stripped after it was installed is listed with the hash
and size it has. Nothing in the image depends on a file that is not in it.
Most runtime packages are installed only as prebuilt wheels; the few without one
(`SDIST_ONLY` in `appimage/build.sh`) are built from their pinned sources with the pinned tools.
Two of them, gssapi and krb5, are Cython: Cython 3.3.0 picks between equally valid C by the order
of a set that hashes by memory address, so `appimage/cython-site/sitecustomize.py` (on the path
for those builds only) has it go in source order instead.

## Verifying a release

1. Check out the release's tag.
2. `packaging/build-appimage.sh build`.
3. Compare `sha256sum dist/DAToolkit-*.AppImage` with the release's `.sha256`.

If they differ, `diffoscope` on the two files shows where.

## Testing what ships

A developer's venv is not the AppImage: its packages are the newest of the day it was made (and
the distribution's own, when it was made with `--system-site-packages`), on the system's
Python. `packaging/test-locked.sh` runs the test suite on the AppImage instead:

```bash
packaging/test-locked.sh                         # the whole suite, on dist/'s newest AppImage
packaging/test-locked.sh -q tests/test_images.py # any pytest arguments
APPIMAGE=/elsewhere/DAToolkit-<version>-x86_64.AppImage packaging/test-locked.sh
```

It unpacks the AppImage into a temporary folder and has its Python (`-I`, as AppRun runs it)
run the checkout's `tests/` against the `datoolkit` and the packages installed in the image.
The image must be of the checkout: if a file of its `datoolkit` is not the one in
`src/datoolkit`, the script names it and tests nothing. pytest and pytest-asyncio are not in
the image; they are installed from `appimage/test-requirements.lock`, with hashes, into a
folder of their own that comes last on the path, so that nothing they bring stands in for a
package that ships. The pip that installs them is the pinned Python's the build uses
(`pins.sh`), taken from the build's cache or downloaded and checked as the build does. The
suite runs under a 3 GB memory cap where there is a systemd user session. Tests that need a
display, Xvfb or Node skip without them, as anywhere. The script needs git, GNU tar, curl and
the network (for those few wheels).

## Releasing

1. Update the version in `pyproject.toml` and `src/datoolkit/__init__.py`, commit, tag.
2. `packaging/build-appimage.sh check` (reproducible), then `packaging/test-locked.sh` (the
   test suite passes on what was built) and `packaging/test-appimage.sh` (it starts on each
   distribution).
3. Attach the AppImage and its `.sha256` to the release.

Rebuild and re-release when a pinned dependency has a security fix: the locks don't move by
themselves. WebKitGTK and GTK update with the system.
