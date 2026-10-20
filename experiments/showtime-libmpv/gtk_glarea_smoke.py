import ctypes,os,sys,traceback
import gi
gi.require_version("Gtk","4.0")
from gi.repository import Gtk,Gdk,GLib
from OpenGL import GL
from mpv import MPV,MpvRenderContext,MpvGlGetProcAddressFn

egl=ctypes.CDLL("libEGL.so.1")
egl.eglGetProcAddress.argtypes=[ctypes.c_char_p]
egl.eglGetProcAddress.restype=ctypes.c_void_p
glx=ctypes.CDLL("libGLX.so.0")
glx.glXGetProcAddressARB.argtypes=[ctypes.c_char_p]
glx.glXGetProcAddressARB.restype=ctypes.c_void_p
def proc_addr(ctx,name):
    return glx.glXGetProcAddressARB(name) or egl.eglGetProcAddress(name)

class Player(Gtk.GLArea):
    def __init__(self):
        super().__init__(hexpand=True,vexpand=True)
        self.set_auto_render(False)
        self.ctx=None
        self.mpv=MPV(vo="libmpv",ytdl="no", hwdec="no", audio="no")
        self.callback=MpvGlGetProcAddressFn(proc_addr)
        self.frames=0
        self.connect("realize",self.on_realize)
        self.connect("render",self.on_render)
        self.connect("unrealize",self.on_unrealize)

    def on_realize(self,*args):
        try:
            self.make_current()
            self.ctx=MpvRenderContext(self.mpv,"opengl",opengl_init_params={"get_proc_address":self.callback})
            self.ctx.update_cb=self.notify_frame
            self.mpv.play(sys.argv[1])
            print("RENDER_CONTEXT_STARTED",flush=True)
        except Exception:
            traceback.print_exc()

    def notify_frame(self):
        GLib.idle_add(self.queue_render)

    def on_render(self,*args):
        if not self.ctx:return False
        try:
            factor=self.get_scale_factor()
            w,h=self.get_width()*factor,self.get_height()*factor
            fbo=GL.glGetIntegerv(GL.GL_DRAW_FRAMEBUFFER_BINDING)
            self.ctx.render(flip_y=True,opengl_fbo={"w":w,"h":h,"fbo":int(fbo)})
            self.frames+=1
            return True
        except Exception:
            traceback.print_exc()
            return False

    def on_unrealize(self,*args):
        if self.ctx:self.ctx.free();self.ctx=None
        self.mpv.terminate()

app=Gtk.Application(application_id="com.example.ShowtimeMpvRenderingProof")
def on_activate(a):
    win=Gtk.ApplicationWindow(application=a, title="Native GTK4 + libmpv test")
    win.set_default_size(640,360)
    player=Player()
    win.set_child(player)
    win.present()
    def finish():
        print("GTK_LIBMPV_FRAME_COUNT",player.frames,"POSITION",player.mpv.time_pos,flush=True)
        win.close()
        a.quit()
        return False
    GLib.timeout_add_seconds(5,finish)
app.connect("activate",on_activate)
app.run([])
