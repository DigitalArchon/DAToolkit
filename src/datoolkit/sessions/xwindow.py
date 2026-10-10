"""Windows on this desktop (X11): list them and capture one as a PNG.

A window session is a window of another program (a ScreenConnect, TeamViewer or AnyDesk
control window, a VM or iDRAC console) that the technician wants the AI to see. It is found
by its X window id, so moving or resizing it doesn't matter. Nothing is ever sent to the
window: this module only reads.

It talks to the X server through libxcb with ctypes, which every X11 desktop has (libX11 is
built on it), so there is no extra dependency. XCB rather than Xlib: Xlib reports errors
through one process-wide handler, which GTK in the same process relies on; XCB returns them
per request. Each call opens its own connection, so it is safe from any thread.

Wayland has no way to read another program's window. Under XWayland, X programs (the
ScreenConnect client among them) are still listed and captured; native Wayland ones are not."""

from __future__ import annotations

import ctypes
import ctypes.util
import io
import os
import re
import sys
from ctypes import POINTER, Structure, byref, c_char_p, c_int, c_uint8, c_uint16, c_uint32, c_void_p
from dataclasses import dataclass

MAX_SIDE = 8192                 # larger than any real window; a bigger one is a broken reply
MAP_VIEWABLE = 2
Z_PIXMAP = 2
ANY_PROPERTY_TYPE = 0

# Programs a technician is likely to want: listed first in the picker.
REMOTE_TOOLS = ("screenconnect", "connectwise", "teamviewer", "anydesk", "splashtop", "rustdesk",
                "vnc", "remmina", "virt-viewer", "remote-viewer", "vmware", "virtualbox", "idrac",
                "ilo", "kvm", "parsec", "nomachine", "nxplayer", "dwservice", "zoho", "bomgar",
                "beyondtrust", "logmein", "rescue")


class WindowError(Exception):
    pass


@dataclass
class WindowInfo:
    xid: int
    title: str
    wm_class: str          # the class part of WM_CLASS, e.g. "ScreenConnect.WindowsClient"
    instance: str          # the instance part
    pid: int
    width: int
    height: int
    visible: bool

    @property
    def remote_tool(self) -> bool:
        text = f"{self.title} {self.wm_class} {self.instance}".lower()
        return any(t in text for t in REMOTE_TOOLS)

    @property
    def short_name(self) -> str:
        """A session name from the title: the first part that isn't the tool's own name, e.g.
        "ScreenConnect - ACME-PC07 - Administrator" -> "ACME-PC07"."""
        tool = re.compile("(?:" + "|".join(REMOTE_TOOLS) + r"|control|remote|desktop|client|viewer)+", re.I)
        for part in re.split(r"\s+[-–—|:]\s+", self.title):
            if part.strip() and not tool.fullmatch(re.sub(r"[\s.]+", "", part)):
                return part.strip()[:40]
        return (self.title or self.wm_class or "window")[:40]

    def to_dict(self) -> dict:
        return {"xid": self.xid, "title": self.title, "wm_class": self.wm_class, "instance": self.instance,
                "width": self.width, "height": self.height, "visible": self.visible,
                "remote_tool": self.remote_tool, "short_name": self.short_name}


# ---------------------------------------------------------------- libxcb

class _Cookie(Structure):
    _fields_ = [("sequence", ctypes.c_uint)]


class _Error(Structure):
    _fields_ = [("response_type", c_uint8), ("error_code", c_uint8), ("sequence", c_uint16),
                ("resource_id", c_uint32), ("minor_code", c_uint16), ("major_code", c_uint8),
                ("pad0", c_uint8), ("pad", c_uint32 * 5), ("full_sequence", c_uint32)]


class _InternAtomReply(Structure):
    _fields_ = [("response_type", c_uint8), ("pad0", c_uint8), ("sequence", c_uint16), ("length", c_uint32),
                ("atom", c_uint32)]


class _GetPropertyReply(Structure):
    _fields_ = [("response_type", c_uint8), ("format", c_uint8), ("sequence", c_uint16), ("length", c_uint32),
                ("type", c_uint32), ("bytes_after", c_uint32), ("value_len", c_uint32), ("pad0", c_uint8 * 12)]


class _AttributesReply(Structure):
    _fields_ = [("response_type", c_uint8), ("backing_store", c_uint8), ("sequence", c_uint16),
                ("length", c_uint32), ("visual", c_uint32), ("class_", c_uint16), ("bit_gravity", c_uint8),
                ("win_gravity", c_uint8), ("backing_planes", c_uint32), ("backing_pixel", c_uint32),
                ("save_under", c_uint8), ("map_is_installed", c_uint8), ("map_state", c_uint8),
                ("override_redirect", c_uint8), ("colormap", c_uint32), ("all_event_masks", c_uint32),
                ("your_event_mask", c_uint32), ("do_not_propagate_mask", c_uint16), ("pad0", c_uint8 * 2)]


class _GeometryReply(Structure):
    _fields_ = [("response_type", c_uint8), ("depth", c_uint8), ("sequence", c_uint16), ("length", c_uint32),
                ("root", c_uint32), ("x", ctypes.c_int16), ("y", ctypes.c_int16), ("width", c_uint16),
                ("height", c_uint16), ("border_width", c_uint16), ("pad0", c_uint8 * 2)]


class _QueryTreeReply(Structure):
    _fields_ = [("response_type", c_uint8), ("pad0", c_uint8), ("sequence", c_uint16), ("length", c_uint32),
                ("root", c_uint32), ("parent", c_uint32), ("children_len", c_uint16), ("pad1", c_uint8 * 14)]


class _SelectionOwnerReply(Structure):
    _fields_ = [("response_type", c_uint8), ("pad0", c_uint8), ("sequence", c_uint16), ("length", c_uint32),
                ("owner", c_uint32)]


class _ImageReply(Structure):
    _fields_ = [("response_type", c_uint8), ("depth", c_uint8), ("sequence", c_uint16), ("length", c_uint32),
                ("visual", c_uint32), ("pad0", c_uint8 * 20)]


class _Setup(Structure):
    _fields_ = [("status", c_uint8), ("pad0", c_uint8), ("protocol_major_version", c_uint16),
                ("protocol_minor_version", c_uint16), ("length", c_uint16), ("release_number", c_uint32),
                ("resource_id_base", c_uint32), ("resource_id_mask", c_uint32), ("motion_buffer_size", c_uint32),
                ("vendor_len", c_uint16), ("maximum_request_length", c_uint16), ("roots_len", c_uint8),
                ("pixmap_formats_len", c_uint8), ("image_byte_order", c_uint8),
                ("bitmap_format_bit_order", c_uint8), ("bitmap_format_scanline_unit", c_uint8),
                ("bitmap_format_scanline_pad", c_uint8), ("min_keycode", c_uint8), ("max_keycode", c_uint8),
                ("pad1", c_uint8 * 4)]


class _Format(Structure):
    _fields_ = [("depth", c_uint8), ("bits_per_pixel", c_uint8), ("scanline_pad", c_uint8), ("pad0", c_uint8 * 5)]


class _Screen(Structure):
    _fields_ = [("root", c_uint32)]       # first field only; never allocated or sized here


class _ScreenIterator(Structure):
    _fields_ = [("data", POINTER(_Screen)), ("rem", c_int), ("index", c_int)]


_lib = None


def _xcb():
    global _lib
    if _lib is not None:
        return _lib
    name = ctypes.util.find_library("xcb") or "libxcb.so.1"
    try:
        lib = ctypes.CDLL(name)
    except OSError as e:
        raise WindowError("libxcb isn't available, so windows can't be read (window sessions need X11).") from e
    libc = ctypes.CDLL(None)
    lib.free = libc.free
    lib.free.argtypes, lib.free.restype = [c_void_p], None

    def fn(name, restype, *argtypes):
        f = getattr(lib, name)
        f.restype, f.argtypes = restype, list(argtypes)

    conn = c_void_p
    fn("xcb_connect", conn, c_char_p, POINTER(c_int))
    fn("xcb_disconnect", None, conn)
    fn("xcb_connection_has_error", c_int, conn)
    fn("xcb_get_setup", POINTER(_Setup), conn)
    fn("xcb_setup_roots_iterator", _ScreenIterator, POINTER(_Setup))
    fn("xcb_screen_next", None, POINTER(_ScreenIterator))
    fn("xcb_setup_pixmap_formats", POINTER(_Format), POINTER(_Setup))
    fn("xcb_setup_pixmap_formats_length", c_int, POINTER(_Setup))
    fn("xcb_intern_atom", _Cookie, conn, c_uint8, c_uint16, c_char_p)
    fn("xcb_intern_atom_reply", POINTER(_InternAtomReply), conn, _Cookie, POINTER(POINTER(_Error)))
    fn("xcb_get_property", _Cookie, conn, c_uint8, c_uint32, c_uint32, c_uint32, c_uint32, c_uint32)
    fn("xcb_get_property_reply", POINTER(_GetPropertyReply), conn, _Cookie, POINTER(POINTER(_Error)))
    fn("xcb_get_property_value", c_void_p, POINTER(_GetPropertyReply))
    fn("xcb_get_property_value_length", c_int, POINTER(_GetPropertyReply))
    fn("xcb_get_window_attributes", _Cookie, conn, c_uint32)
    fn("xcb_get_window_attributes_reply", POINTER(_AttributesReply), conn, _Cookie, POINTER(POINTER(_Error)))
    fn("xcb_get_geometry", _Cookie, conn, c_uint32)
    fn("xcb_get_geometry_reply", POINTER(_GeometryReply), conn, _Cookie, POINTER(POINTER(_Error)))
    fn("xcb_query_tree", _Cookie, conn, c_uint32)
    fn("xcb_query_tree_reply", POINTER(_QueryTreeReply), conn, _Cookie, POINTER(POINTER(_Error)))
    fn("xcb_query_tree_children", POINTER(c_uint32), POINTER(_QueryTreeReply))
    fn("xcb_query_tree_children_length", c_int, POINTER(_QueryTreeReply))
    fn("xcb_get_selection_owner", _Cookie, conn, c_uint32)
    fn("xcb_get_selection_owner_reply", POINTER(_SelectionOwnerReply), conn, _Cookie, POINTER(POINTER(_Error)))
    fn("xcb_get_image", _Cookie, conn, c_uint8, c_uint32, ctypes.c_int16, ctypes.c_int16, c_uint16, c_uint16, c_uint32)
    fn("xcb_get_image_reply", POINTER(_ImageReply), conn, _Cookie, POINTER(POINTER(_Error)))
    fn("xcb_get_image_data", POINTER(c_uint8), POINTER(_ImageReply))
    fn("xcb_get_image_data_length", c_int, POINTER(_ImageReply))
    _lib = lib
    return lib


def available() -> str:
    """'' when windows can be read here, else why not."""
    if not os.environ.get("DISPLAY"):
        if os.environ.get("WAYLAND_DISPLAY"):
            return ("This is a Wayland session without XWayland: other programs' windows can't be read. "
                    "Log in to an X11 session to use window sessions.")
        return "No X display: window sessions need an X11 desktop."
    try:
        _xcb()
    except WindowError as e:
        return str(e)
    return ""


class _Conn:
    """One connection to the X server, closed on exit."""

    def __init__(self):
        why = available()
        if why:
            raise WindowError(why)
        self.x = _xcb()
        screen = c_int(0)
        self.c = self.x.xcb_connect(None, byref(screen))
        if not self.c or self.x.xcb_connection_has_error(self.c):
            if self.c:
                self.x.xcb_disconnect(self.c)
            raise WindowError(f"Can't connect to the X display {os.environ.get('DISPLAY')}.")
        self.screen = screen.value
        setup = self.x.xcb_get_setup(self.c)
        it = self.x.xcb_setup_roots_iterator(setup)
        for _ in range(self.screen):
            self.x.xcb_screen_next(byref(it))
        self.root = it.data.contents.root
        self.lsb_first = setup.contents.image_byte_order == 0
        fmts = self.x.xcb_setup_pixmap_formats(setup)
        self.bpp = {fmts[i].depth: fmts[i].bits_per_pixel
                    for i in range(self.x.xcb_setup_pixmap_formats_length(setup))}
        self._atoms: dict[str, int] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.x.xcb_disconnect(self.c)

    def _reply(self, name: str, cookie):
        err = POINTER(_Error)()
        r = getattr(self.x, f"{name}_reply")(self.c, cookie, byref(err))
        if err:
            self.x.free(err)
        return r or None

    def atom(self, name: str) -> int:
        if name not in self._atoms:
            r = self._reply("xcb_intern_atom", self.x.xcb_intern_atom(self.c, 0, len(name), name.encode()))
            self._atoms[name] = r.contents.atom if r else 0
            if r:
                self.x.free(r)
        return self._atoms[name]

    def prop(self, win: int, name: str, length: int = 4096) -> tuple[int, int, bytes] | None:
        """(type atom, format, raw bytes) of a property, or None if the window or property is gone."""
        r = self._reply("xcb_get_property", self.x.xcb_get_property(
            self.c, 0, win, self.atom(name), ANY_PROPERTY_TYPE, 0, length))
        if not r:
            return None
        try:
            if not r.contents.type:
                return None
            n = self.x.xcb_get_property_value_length(r)
            return r.contents.type, r.contents.format, ctypes.string_at(self.x.xcb_get_property_value(r), n)
        finally:
            self.x.free(r)

    def windows_prop(self, win: int, name: str) -> list[int]:
        p = self.prop(win, name, 1 << 16)
        if not p or p[1] != 32:
            return []
        data = p[2]
        # property values arrive in this client's byte order (image data in the server's)
        return [int.from_bytes(data[i:i + 4], sys.byteorder) for i in range(0, len(data), 4)]

    def attributes(self, win: int):
        r = self._reply("xcb_get_window_attributes", self.x.xcb_get_window_attributes(self.c, win))
        if not r:
            return None
        try:
            return r.contents.map_state, r.contents.override_redirect
        finally:
            self.x.free(r)

    def geometry(self, win: int):
        r = self._reply("xcb_get_geometry", self.x.xcb_get_geometry(self.c, win))
        if not r:
            return None
        try:
            return r.contents.width, r.contents.height, r.contents.depth
        finally:
            self.x.free(r)

    def children(self, win: int) -> list[int]:
        r = self._reply("xcb_query_tree", self.x.xcb_query_tree(self.c, win))
        if not r:
            return []
        try:
            kids = self.x.xcb_query_tree_children(r)
            return [kids[i] for i in range(self.x.xcb_query_tree_children_length(r))]
        finally:
            self.x.free(r)

    def composited(self) -> bool:
        """Whether a compositing manager runs: then a covered window still has its own pixels."""
        r = self._reply("xcb_get_selection_owner",
                        self.x.xcb_get_selection_owner(self.c, self.atom(f"_NET_WM_CM_S{self.screen}")))
        if not r:
            return False
        try:
            return bool(r.contents.owner)
        finally:
            self.x.free(r)

    def text(self, win: int, *names: str) -> str:
        for name in names:
            p = self.prop(win, name)
            if p and p[2]:
                enc = "utf-8" if p[0] == self.atom("UTF8_STRING") else "latin-1"
                return p[2].split(b"\0")[0].decode(enc, "replace").strip()
        return ""

    def info(self, win: int) -> WindowInfo | None:
        attrs, geo = self.attributes(win), self.geometry(win)
        if attrs is None or geo is None:
            return None
        cls = self.prop(win, "WM_CLASS")
        parts = (cls[2].split(b"\0") if cls else []) + [b"", b""]
        pid = self.prop(win, "_NET_WM_PID")
        hidden = self.atom("_NET_WM_STATE_HIDDEN") in self.windows_prop(win, "_NET_WM_STATE")
        return WindowInfo(
            xid=win, title=self.text(win, "_NET_WM_NAME", "WM_NAME"),
            instance=parts[0].decode("latin-1", "replace"), wm_class=parts[1].decode("latin-1", "replace"),
            pid=int.from_bytes(pid[2][:4], sys.byteorder) if pid and len(pid[2]) >= 4 else 0,
            width=geo[0], height=geo[1], visible=attrs[0] == MAP_VIEWABLE and not hidden)


def list_windows(exclude_pid: int | None = None) -> list[WindowInfo]:
    """Top-level windows with a title or class, remote-support tools first. Uses the window
    manager's client list; without a window manager, the root window's mapped children."""
    with _Conn() as x:
        ids = x.windows_prop(x.root, "_NET_CLIENT_LIST")
        if not ids:
            ids = [w for w in x.children(x.root) if (a := x.attributes(w)) and a[0] == MAP_VIEWABLE and not a[1]]
        out = []
        for w in ids:
            info = x.info(w)
            if not info or not (info.title or info.wm_class) or (exclude_pid and info.pid == exclude_pid):
                continue
            out.append(info)
    out.sort(key=lambda i: (not i.remote_tool, not i.visible, i.title.lower()))
    return out


def window_info(xid: int) -> WindowInfo | None:
    """The window's current details, or None once it has closed."""
    with _Conn() as x:
        return x.info(xid)


def capture(xid: int) -> tuple[bytes, dict]:
    """PNG of the window's contents (without the title bar and frame), and notes about the
    capture. Raises WindowError when it can't be captured, saying what to do."""
    from PIL import Image

    with _Conn() as x:
        info = x.info(xid)
        if info is None:
            raise WindowError("The window has closed.")
        if not info.visible:
            raise WindowError("The window is minimised or on another workspace: bring it back on screen, "
                              "then take the screenshot again.")
        w, h, depth = x.geometry(xid)
        if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE):
            raise WindowError(f"The window has an unusable size ({w}×{h}).")
        bpp = x.bpp.get(depth)
        if depth not in (24, 32) or bpp != 32:
            raise WindowError(f"The window uses a {depth}-bit colour depth, which isn't supported "
                              "(24 or 32-bit colour is).")
        composited = x.composited()
        r = x._reply("xcb_get_image", x.x.xcb_get_image(x.c, Z_PIXMAP, xid, 0, 0, w, h, 0xFFFFFFFF))
        if not r:
            raise WindowError("The X server wouldn't hand over the window's pixels. Move the window so all "
                              "of it is on the screen, then try again." if not composited
                              else "The X server wouldn't hand over the window's pixels.")
        try:
            n = x.x.xcb_get_image_data_length(r)
            data = ctypes.string_at(x.x.xcb_get_image_data(r), n)
        finally:
            x.x.free(r)
        if n < w * h * 4:
            raise WindowError("The X server returned an incomplete image.")
        img = Image.frombuffer("RGB", (w, h), data, "raw", "BGRX" if x.lsb_first else "XRGB", w * 4, 1)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        notes = {"width": w, "height": h, "title": info.title, "composited": composited}
        return buf.getvalue(), notes
