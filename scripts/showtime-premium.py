#!/usr/bin/python3
"""PremiumMedia launcher for GNOME Showtime with automatic external subtitles."""

import gettext
import locale
import os
import signal
import sys
from pathlib import Path
from platform import system

PKG_DATA_DIR = "/app/share/showtime"
LOCALE_DIR = "/app/share/locale"

sys.path.insert(1, PKG_DATA_DIR)
signal.signal(signal.SIGINT, signal.SIG_DFL)

if system() == "Linux":
    locale.bindtextdomain("showtime", LOCALE_DIR)
    locale.textdomain("showtime")
    gettext.install("showtime", LOCALE_DIR)
else:
    gettext.install("showtime")

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

GLib.set_prgname("showtime")
GLib.set_application_name(_("Video Player"))
Gtk.Window.set_default_icon_name("org.gnome.Showtime")
Gio.resources_register(
    Gio.Resource.load(str(Path(PKG_DATA_DIR, "showtime.gresource")))
)

from showtime.widgets.window import Window

_original_play_video = Window.play_video


def _premium_play_video(self, gfile):
    subtitle_uri = os.environ.get("PREMIUM_MEDIA_SUBTITLE_URI", "").strip()

    # GstPlay's native external subtitle input. Set it before the video URI so
    # discovery includes the subtitle stream from the beginning.
    if subtitle_uri:
        try:
            self.play.props.suburi = subtitle_uri
        except Exception as exc:
            print(f"PremiumMedia: failed to set subtitle URI: {exc}", file=sys.stderr)

    _original_play_video(self, gfile)

    if subtitle_uri:
        # Media-info arrives asynchronously. Re-assert the track after startup
        # so Showtime's normal subtitle action/menu is enabled and track 0 is
        # selected rather than leaving external captions disabled.
        def enable_subtitles():
            try:
                self.play.props.suburi = subtitle_uri
                self.select_subtitles(0)
            except Exception as exc:
                print(f"PremiumMedia: failed to enable subtitles: {exc}", file=sys.stderr)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(250, enable_subtitles)


Window.play_video = _premium_play_video

from showtime import main

raise SystemExit(main.main())
