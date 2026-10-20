"""Experimental GTK4 libmpv video surface for GNOME Showtime host builds.

Never auto-enabled; no fallback UI or persistent settings are changed.
The original GNOME Showtime overlay owns all window chrome.
"""
from __future__ import annotations
import ctypes
import logging
from gi.repository import GLib, Gtk
from OpenGL import GL
from mpv import MPV, MpvRenderContext, MpvGlGetProcAddressFn

_glx=ctypes.CDLL("libGLX.so.0")
_glx.glXGetProcAddressARB.argtypes=[ctypes.c_char_p]
_glx.glXGetProcAddressARB.restype=ctypes.c_void_p
_egl=ctypes.CDLL("libEGL.so.1")
_egl.eglGetProcAddress.argtypes=[ctypes.c_char_p]
_egl.eglGetProcAddress.restype=ctypes.c_void_p

def get_proc_address(_context,name):
    return _glx.glXGetProcAddressARB(name) or _egl.eglGetProcAddress(name)

class MpvGlArea(Gtk.GLArea):
    def __init__(self,media_uri=None):
        super().__init__(hexpand=True,vexpand=True)
        self.set_auto_render(False)
        self._proc_addr_callback=MpvGlGetProcAddressFn(get_proc_address)
        self.mpv=MPV(vo="libmpv", ytdl="no", hwdec="no", audio="no")
        self.ctx=None
        self.frames=0
        self.media_uri=media_uri
        self.connect("realize",self._realize)
        self.connect("render",self._render)
        self.connect("unrealize",self._unrealize)

    def _realize(self,_area):
        self.make_current()
        if self.get_error():
            print("SHOWTIME_LIBMPV_CONTEXT_ERROR",flush=True)
            return
        try:
            self.ctx=MpvRenderContext(self.mpv,"opengl",
                opengl_init_params={"get_proc_address":self._proc_addr_callback})
            self.ctx.update_cb=self._render_requested
            if self.media_uri:
                self.mpv.play(self.media_uri)
            print("SHOWTIME_LIBMPV_CONTEXT_READY",flush=True)
        except Exception:
            logging.exception("Showtime: libmpv OpenGL setup failed")

    def _render_requested(self):
        GLib.idle_add(self.queue_render)

    def _render(self,_area,_context):
        if not self.ctx:return False
        try:
            sf=self.get_scale_factor()
            fbo=int(GL.glGetIntegerv(GL.GL_DRAW_FRAMEBUFFER_BINDING))
            self.ctx.render(flip_y=True,opengl_fbo={
                "w":self.get_width()*sf,"h":self.get_height()*sf,"fbo":fbo})
            self.frames+=1
            return True
        except Exception:
            logging.exception("Showtime: libmpv render failed")
            return False

    def _unrealize(self,_area):
        if self.ctx:
            self.ctx.free()
            self.ctx=None
        self.mpv.terminate()

def install_area(window, uri):
    parent=window.picture.get_parent()
    if not isinstance(parent,Gtk.GraphicsOffload):
        raise RuntimeError(f"Unexpected Showtime video parent: {type(parent).__name__}")
    area=MpvGlArea(uri)
    # Showtime subtitle/study overlay and chrome are two levels up. Only
    # the actual picture is replaced — zero native sidecar windows.
    parent.set_child(area)
    window._experimental_libmpv_area=area
    return area

class MpvProps:
    """Minimal GstPlay property contract backed by mpv's own playback state."""
    def __init__(self,area):
        self.area=area
        self._uri=None
        self._suburi=None

    @property
    def position(self):
        return int((self.area.mpv.time_pos or 0)*1_000_000_000)

    @property
    def duration(self):
        return int((self.area.mpv.duration or 0)*1_000_000_000)

    @property
    def uri(self):
        return self._uri

    @uri.setter
    def uri(self, value):
        self._uri=value
        self.area.mpv.play(value)

    @property
    def volume(self):
        return max(0.0,min(1.0,(self.area.mpv.volume or 0)/100))

    @volume.setter
    def volume(self,value):
        self.area.mpv.volume=max(0,min(100,int(float(value)*100)))

    @property
    def mute(self):
        return bool(self.area.mpv.mute)

    @mute.setter
    def mute(self,value):
        self.area.mpv.mute=bool(value)

    @property
    def rate(self):
        return float(self.area.mpv.speed or 1)

    @rate.setter
    def rate(self,value):
        self.area.mpv.speed=float(value)

    @property
    def suburi(self):
        return self._suburi

    @suburi.setter
    def suburi(self,value):
        self._suburi=value
        if value:
            self.area.mpv.command('sub-add',value,'select')


class MpvPlayFacade:
    """The initial facade needed by Showtime's native media controls."""
    def __init__(self,area):
        self.area=area
        self.props=MpvProps(area)

    def play(self):
        self.area.mpv.pause=False

    def pause(self):
        self.area.mpv.pause=True

    def seek(self,nanoseconds):
        self.area.mpv.seek(float(nanoseconds)/1_000_000_000,reference='absolute')

    def stop(self):
        self.area.mpv.stop()

    def set_volume(self,volume):
        self.props.volume=volume

    def get_position(self):
        return self.props.position

    def get_uri(self):
        return self.props.uri

    def get_media_info(self):
        # TODO: implement GstPlay compatible media metadata adapter,
        # including the audio/subtitle track menus. Do not fake metadata.
        return None

    def get_volume(self):
        return self.props.volume

    def set_subtitle_track_enabled(self,enabled):
        if not enabled:self.area.mpv.sid='no'

    def set_subtitle_track(self,index):
        self.area.mpv.sid=index

    def set_audio_track(self,index):
        self.area.mpv.aid=index


def install_control_facade(window,area):
    from gi.repository import GstPlay
    facade=MpvPlayFacade(area)
    window._original_gst_play=window.play
    window.play=facade
    def update():
        if area.get_parent() is None:
            return GLib.SOURCE_REMOVE
        duration=facade.props.duration
        if duration>0:
            window._decoder_state='paused' if area.mpv.pause else 'playing'
            window._decoder_position=facade.props.position
            window._decoder_duration=duration
            window._decoder_video_streams=1
            window._decoder_audio_streams=1
            window._decoder_seekable=True
            window._on_position_updated(None,facade.props.position)
            window._on_playback_state_changed(
                None, GstPlay.PlayState.PAUSED if area.mpv.pause else GstPlay.PlayState.PLAYING)
        return GLib.SOURCE_CONTINUE
    GLib.timeout_add(250,update)
    return facade
