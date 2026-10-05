#!/usr/bin/python3
"""GNOME Showtime launcher extended for ani-cli episode sessions."""
from __future__ import annotations
import bisect, gettext, json, locale, os, re, signal, sys, threading, urllib.parse, urllib.request
from pathlib import Path
from platform import system

PKG_DATA_DIR="/app/share/showtime"
LOCALE_DIR="/app/share/locale"
SESSION_URL=os.environ.get("MEDIA_PLAYER_SESSION_URL","").strip()
SESSION_BASE=SESSION_URL.rsplit("/api/session",1)[0] if SESSION_URL else ""
INITIAL_SUBTITLE_URI=os.environ.get("MEDIA_PLAYER_SUBTITLE_URI","").strip()
AUTOPLAY_DEFAULT=os.environ.get("MEDIA_PLAYER_AUTOPLAY","1")!="0"

sys.path.insert(1,PKG_DATA_DIR)
signal.signal(signal.SIGINT,signal.SIG_DFL)
if system()=="Linux":
    locale.bindtextdomain("showtime",LOCALE_DIR); locale.textdomain("showtime"); gettext.install("showtime",LOCALE_DIR)
else:
    gettext.install("showtime")

import gi
gi.require_version("Gtk","4.0")
gi.require_version("Adw","1")
from gi.repository import Adw,Gio,GLib,Gtk
GLib.set_prgname("showtime"); GLib.set_application_name(_("Video Player"))
Gtk.Window.set_default_icon_name("org.gnome.Showtime")
Gio.resources_register(Gio.Resource.load(str(Path(PKG_DATA_DIR,"showtime.gresource"))))
from showtime.widgets.options import Options
from showtime.widgets.window import Window

_original_init=Window.__init__
_original_play_video=Window.play_video
_original_position_updated=Window._on_position_updated
_original_media_info_updated=Window._on_media_info_updated
_original_end_of_stream=Window._on_end_of_stream
_original_build_menus=Options.build_menus

def _http_json(url,method="GET",payload=None,timeout=45):
    data=None; headers={"Accept":"application/json"}
    if payload is not None:
        data=json.dumps(payload).encode(); headers["Content-Type"]="application/json"
    req=urllib.request.Request(url,data=data,headers=headers,method=method)
    with urllib.request.urlopen(req,timeout=timeout) as response:
        value=json.loads(response.read().decode("utf-8","replace"))
    if not isinstance(value,dict): raise RuntimeError("session controller returned invalid JSON")
    if value.get("ok") is False: raise RuntimeError(str(value.get("error") or "session controller error"))
    return value

def _toast(window,message):
    try: window.toast_overlay.add_toast(Adw.Toast.new(message))
    except Exception: print(f"MediaPlayer: {message}",file=sys.stderr)

def _vtt_seconds(value):
    parts=value.strip().replace(",",".").split(":")
    try:
        if len(parts)==3:
            hours,minutes,seconds=parts
        elif len(parts)==2:
            hours="0"; minutes,seconds=parts
        else:
            return None
        return int(hours)*3600+int(minutes)*60+float(seconds)
    except (TypeError,ValueError):
        return None

def _parse_vtt(data):
    text=data.decode("utf-8-sig","replace").replace("\r\n","\n").replace("\r","\n")
    cues=[]
    blocks=re.split(r"\n{2,}",text)
    for block in blocks:
        lines=[line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0].upper()=="WEBVTT":
            continue
        timing_index=next((i for i,line in enumerate(lines) if "-->" in line),None)
        if timing_index is None:
            continue
        left,right=lines[timing_index].split("-->",1)
        start=_vtt_seconds(left)
        end=_vtt_seconds(right.split()[0])
        if start is None or end is None or end<=start:
            continue
        body="\n".join(lines[timing_index+1:])
        body=re.sub(r"<[^>]+>","",body).strip()
        if body:
            cues.append((int(start*1_000_000_000),int(end*1_000_000_000),body))
    cues.sort(key=lambda item:item[0])
    return cues


def _parse_clock(value):
    value=str(value).strip().replace(',', '.')
    parts=value.split(':')
    try:
        if len(parts)==3:
            return float(parts[0])*3600+float(parts[1])*60+float(parts[2])
        if len(parts)==2:
            return float(parts[0])*60+float(parts[1])
        return float(parts[0])
    except Exception:
        return 0.0


def _parse_srt(data):
    text=data.decode('utf-8-sig','replace').replace('\r','')
    cues=[]
    for block in re.split(r'\n{2,}',text):
        lines=[line.strip() for line in block.splitlines() if line.strip()]
        timing=next((i for i,line in enumerate(lines) if '-->' in line),None)
        if timing is None:
            continue
        left,right=lines[timing].split('-->',1)
        body='\n'.join(lines[timing+1:])
        body=re.sub(r'<[^>]+>','',body).strip()
        if body:
            cues.append((int(_parse_clock(left)*1e9),int(_parse_clock(right.split()[0])*1e9),body))
    cues.sort(key=lambda item:item[0])
    return cues


def _parse_ass(data):
    text=data.decode('utf-8-sig','replace').replace('\r','')
    fields=None
    cues=[]
    in_events=False
    for raw in text.splitlines():
        line=raw.strip()
        if line.startswith('['):
            in_events=line.lower()=='[events]'
            continue
        if not in_events:
            continue
        if line.lower().startswith('format:'):
            fields=[x.strip().lower() for x in line.split(':',1)[1].split(',')]
            continue
        if not line.lower().startswith('dialogue:'):
            continue
        payload=line.split(':',1)[1].lstrip()
        count=len(fields) if fields else 10
        parts=payload.split(',',max(0,count-1))
        if fields and len(parts)>=len(fields):
            row=dict(zip(fields,parts))
            start=row.get('start','0'); end=row.get('end','0'); body=row.get('text','')
        elif len(parts)>=10:
            start,end,body=parts[1],parts[2],parts[-1]
        else:
            continue
        body=re.sub(r'\{[^}]*\}','',body).replace(r'\N','\n').replace(r'\n','\n').strip()
        if body:
            cues.append((int(_parse_clock(start)*1e9),int(_parse_clock(end)*1e9),body))
    cues.sort(key=lambda item:item[0])
    return cues


def _parse_subtitle_file(data,name=''):
    suffix=Path(name).suffix.lower()
    if suffix=='.srt':
        return _parse_srt(data)
    if suffix in ('.ass','.ssa'):
        return _parse_ass(data)
    return _parse_vtt(data)


def _apply_subtitle_cues(window,cues):
    window._subtitle_cues=list(cues or [])
    window._subtitle_starts=[cue[0] for cue in window._subtitle_cues]
    window._subtitle_last_index=-2
    if hasattr(window,'_subtitle_label'):
        window._subtitle_label.set_visible(False)


def _set_external_subtitle(window,uri):
    """Load controller VTT into the GTK overlay; never touch GstPlay suburi."""
    window._pending_subtitle_uri=str(uri or "")
    try:
        window.play.set_subtitle_track_enabled(False)
        window.play.props.suburi=None
    except Exception:
        pass
    if not uri:
        _apply_subtitle_cues(window,[])
        return
    try:
        with urllib.request.urlopen(str(uri),timeout=4) as response:
            data=response.read()
        cues=_parse_vtt(data)
        if not cues:
            raise RuntimeError("subtitle endpoint returned no usable WebVTT cues")
        _apply_subtitle_cues(window,cues)
    except Exception as exc:
        _apply_subtitle_cues(window,[])
        print(f"MediaPlayer: subtitle overlay load failed: {exc}",file=sys.stderr)

def _update_subtitle_overlay(window,pos_ns):
    label=getattr(window,"_subtitle_label",None)
    cues=getattr(window,"_subtitle_cues",None) or []
    starts=getattr(window,"_subtitle_starts",None) or []
    if label is None or not cues or not starts:
        if label is not None:
            label.set_visible(False)
        return
    pos=int(pos_ns)
    index=bisect.bisect_right(starts,pos)-1
    if index<0 or index>=len(cues) or not (cues[index][0]<=pos<cues[index][1]):
        index=-1
    if index==getattr(window,"_subtitle_last_index",-2):
        return
    window._subtitle_last_index=index
    if index<0:
        label.set_visible(False)
        return
    label.set_text(cues[index][2])
    label.set_visible(True)

def _menu_item(label,action,target):
    item=Gio.MenuItem.new(label,None)
    item.set_action_and_target_value(action,GLib.Variant.new_string(target))
    return item

def _rebuild_subtitle_menu(window):
    session=getattr(window,"_media_session",None) or {}
    if not SESSION_URL or not session:
        return

    menu=window.options.subtitles_menu
    menu.remove_all()
    menu.append_item(_menu_item("None","win.media-subtitle","off"))

    original=Gio.Menu()
    generated=Gio.Menu()
    generated_more=Gio.Menu()
    common_generated={"generated-en","generated-de","generated-ja","generated-ja-romaji"}
    for track in session.get("subtitles",[]):
        track_id=str(track.get("id") or "")
        if not track_id:
            continue
        label=str(track.get("label") or track_id)
        if track.get("source")=="generated" and not track.get("available"):
            status=str(track.get("status") or "idle")
            if status in ("starting","extracting","transcribing","translating","generating"):
                current=track.get("current")
                total=track.get("total")
                if isinstance(current,int) and isinstance(total,int) and total>0:
                    label += f" · {status.title()} {current}/{total}"
                elif status=="extracting":
                    label += " · Extracting…"
                else:
                    label += " · Working…"
            elif status=="error":
                label += " · Retry"
            else:
                label += " · Generate"
        item=_menu_item(label,"win.media-subtitle",track_id)
        if track.get("source")=="generated":
            if track_id in common_generated:
                generated.append_item(item)
            else:
                generated_more.append_item(item)
        else:
            original.append_item(item)

    if generated_more.get_n_items():
        generated.append_submenu("More AI Languages",generated_more)
    if original.get_n_items():
        menu.append_section("Original AniCLI",original)
    if generated.get_n_items():
        menu.append_section("AI Generated",generated)
    custom=getattr(window,"_custom_subtitle",None)
    if custom:
        custom_menu=Gio.Menu()
        custom_menu.append_item(_menu_item(f"Custom · {custom['name']}","win.media-subtitle","custom-local"))
        menu.append_section("Custom",custom_menu)
    menu.append("Add Subtitle File…","win.choose-subtitles")


def _set_session(window,session,refresh_menu=True):
    previous=getattr(window,"_media_session",{}) or {}
    old_episode=str(previous.get("episode") or "")
    old_selected=str(previous.get("selected_subtitle_id") or "")
    window._media_session=session
    new_episode=str(session.get("episode") or "")
    anime_title=str(session.get("anime_title") or "").strip()
    if anime_title and new_episode:
        display_title=f"{anime_title} · Episode {new_episode}"
        try:
            window.title_label.props.label=display_title
            window.set_title(display_title)
        except Exception:
            pass
    new_selected=str(session.get("selected_subtitle_id") or "off")
    if new_episode!=old_episode:
        window._autoplay_cancelled=False
        window._custom_subtitle_active=False
        window._custom_subtitle=None
    elif old_selected and new_selected!=old_selected and not getattr(window,"_custom_subtitle_active",False):
        _set_external_subtitle(window,session.get("subtitle_url"))

    # On the first window load the video starts before /api/session arrives.
    # Apply the controller's persisted per-episode position once, after GstPlay
    # has had a moment to discover the stream.
    if not old_episode and not getattr(window,"_initial_resume_scheduled",False):
        window._initial_resume_scheduled=True
        resume_us=int(session.get("resume_us") or 0)
        revision=session.get("revision")
        if resume_us>5_000_000:
            window._initial_resume_pending=True
            def initial_resume():
                current=getattr(window,"_media_session",{}) or {}
                if current.get("revision")!=revision:
                    window._initial_resume_pending=False
                    return GLib.SOURCE_REMOVE
                try:
                    window.play.seek(resume_us*1000)
                    def release_progress():
                        window._initial_resume_pending=False
                        return GLib.SOURCE_REMOVE
                    GLib.timeout_add(900,release_progress)
                except Exception as exc:
                    window._initial_resume_pending=False
                    print(f"MediaPlayer: initial resume failed: {exc}",file=sys.stderr)
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(650,initial_resume)
    if hasattr(window,"_episode_prev_button"):
        previous_episode=session.get("previous_episode")
        next_episode=session.get("next_episode")
        window._episode_prev_button.set_sensitive(bool(previous_episode) and not window._media_busy)
        window._episode_next_button.set_sensitive(bool(next_episode) and not window._media_busy)
        window._episode_prev_button.set_tooltip_text(f"Previous Episode ({previous_episode})" if previous_episode else "No previous episode")
        window._episode_next_button.set_tooltip_text(f"Next Episode ({next_episode})" if next_episode else "No next episode")
    action=window.lookup_action("media-subtitle")
    if action and not getattr(window,"_custom_subtitle_active",False):
        action.set_state(GLib.Variant.new_string(str(session.get("selected_subtitle_id") or "off")))
    if refresh_menu: _rebuild_subtitle_menu(window)

def _refresh_session_async(window):
    if not SESSION_URL or getattr(window,"_session_refreshing",False): return
    window._session_refreshing=True
    def worker():
        try: session=_http_json(SESSION_URL,timeout=4)
        except Exception as exc:
            session=None; print(f"MediaPlayer: session refresh failed: {exc}",file=sys.stderr)
        def finish():
            window._session_refreshing=False
            if session is not None: _set_session(window,session)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(finish)
    threading.Thread(target=worker,daemon=True).start()

def _poll_session(window):
    _refresh_session_async(window)
    return GLib.SOURCE_CONTINUE

def _post_progress(window):
    if (
        not SESSION_BASE
        or getattr(window,"_progress_posting",False)
        or getattr(window,"_media_busy",False)
        or getattr(window,"_initial_resume_pending",False)
    ):
        return GLib.SOURCE_CONTINUE
    try:
        position_ns=int(window.play.props.position)
        duration_ns=int(window.play.props.duration)
        session=dict(getattr(window,"_media_session",{}) or {})
    except Exception:
        return GLib.SOURCE_CONTINUE
    if position_ns<=0:
        return GLib.SOURCE_CONTINUE
    window._progress_posting=True
    def worker():
        try:
            _http_json(
                f"{SESSION_BASE}/api/progress",
                method="POST",
                payload={
                    "position_ns":position_ns,
                    "duration_ns":duration_ns,
                    "episode":session.get("episode"),
                    "revision":session.get("revision"),
                },
                timeout=4,
            )
        except Exception:
            pass
        def finish():
            window._progress_posting=False
            return GLib.SOURCE_REMOVE
        GLib.idle_add(finish)
    threading.Thread(target=worker,daemon=True).start()
    return GLib.SOURCE_CONTINUE

def _play_session(window,session):
    _set_session(window,session)
    subtitle_uri=session.get("subtitle_url")
    media_url=str(session.get("media_url") or "")
    if not media_url: raise RuntimeError("session has no media URL")
    window._media_stream_ready=False
    window._pending_subtitle_uri=str(subtitle_uri or "")
    window._pending_subtitle_attached=False
    try:
        window.play.set_subtitle_track_enabled(False)
        window.play.props.suburi=None
    except Exception:
        pass
    _original_play_video(window,Gio.File.new_for_uri(media_url))
    _set_external_subtitle(window,subtitle_uri)
    resume_us=int(session.get("resume_us") or 0)
    if resume_us>0:
        window._initial_resume_pending=True
        def resume():
            try:
                window.play.seek(resume_us*1000)
                def release_progress():
                    window._initial_resume_pending=False
                    return GLib.SOURCE_REMOVE
                GLib.timeout_add(900,release_progress)
            except Exception:
                window._initial_resume_pending=False
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(650,resume)

def _navigate(window,direction):
    if not SESSION_BASE or getattr(window,"_media_busy",False): return
    window._media_busy=True
    if hasattr(window,"_episode_prev_button"):
        window._episode_prev_button.set_sensitive(False); window._episode_next_button.set_sensitive(False)
    if hasattr(window,"_next_overlay_button"):
        window._next_overlay_button.set_label("Loading episode…"); window._next_overlay_button.set_visible(True)
    def worker():
        try:
            url=f"{SESSION_BASE}/api/navigate?direction={urllib.parse.quote(direction)}"
            session=_http_json(url,method="POST",payload={},timeout=50); error=None
        except Exception as exc:
            session=None; error=exc
        def finish():
            if error is not None:
                window._media_busy=False
                _toast(window,f"Could not load episode: {error}"); _refresh_session_async(window)
                return GLib.SOURCE_REMOVE
            try:
                _play_session(window,session)
            except Exception as exc:
                _toast(window,f"Could not switch episode: {exc}")
            window._media_busy=False
            return GLib.SOURCE_REMOVE
        GLib.idle_add(finish)
    threading.Thread(target=worker,daemon=True).start()

def _generate_subtitle(window,track_id):
    if not SESSION_BASE: return
    _toast(window,"Generating subtitles in the background…")
    def worker():
        try:
            result=_http_json(f"{SESSION_BASE}/api/subtitles/generate",method="POST",payload={"id":track_id},timeout=8); error=None
        except Exception as exc:
            result=None; error=exc
        def finish():
            if error: _toast(window,f"Subtitle generation could not start: {error}")
            elif result: _set_session(window,result)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(finish)
    threading.Thread(target=worker,daemon=True).start()

def _subtitle_action(window,action,parameter):
    track_id=parameter.get_string()
    if track_id=="custom-local":
        custom=getattr(window,"_custom_subtitle",None)
        if custom:
            window._custom_subtitle_active=True
            _apply_subtitle_cues(window,custom.get("cues",[]))
            action.set_state(GLib.Variant.new_string("custom-local"))
        return
    window._custom_subtitle_active=False
    session=getattr(window,"_media_session",{}) or {}
    track=next((x for x in session.get("subtitles",[]) if str(x.get("id"))==track_id),None)
    if track_id.startswith("generated-") and (not track or not track.get("available")):
        _generate_subtitle(window,track_id); return
    def worker():
        try:
            updated=_http_json(f"{SESSION_BASE}/api/subtitles/select",method="POST",payload={"id":track_id},timeout=8); error=None
        except Exception as exc:
            updated=None; error=exc
        def finish():
            if error:
                _toast(window,f"Could not change subtitles: {error}"); return GLib.SOURCE_REMOVE
            _set_session(window,updated)
            action.set_state(GLib.Variant.new_string(track_id))
            return GLib.SOURCE_REMOVE
        GLib.idle_add(finish)
    threading.Thread(target=worker,daemon=True).start()

def _skip_current_chapter(window):
    chapter=getattr(window,"_active_skip_chapter",None)
    if not chapter: return
    try: window.play.seek(int(float(chapter["end"])*1_000_000_000))
    except Exception as exc: _toast(window,f"Could not skip: {exc}")

def _cancel_autoplay(window):
    window._autoplay_cancelled=True
    if hasattr(window,"_cancel_autoplay_button"): window._cancel_autoplay_button.set_visible(False)
    _toast(window,"Autoplay cancelled for this episode")

def _update_timed_ui(window,pos_ns):
    if not hasattr(window,"_skip_button"): return
    session=getattr(window,"_media_session",{}) or {}
    pos=max(0.0,pos_ns/1_000_000_000)
    duration=max(0.0,float(getattr(window.play.props,"duration",0))/1_000_000_000)
    active=None
    for chapter in session.get("chapters",[]):
        try:
            if float(chapter["start"])<=pos<float(chapter["end"])-0.25:
                active=chapter; break
        except Exception: continue
    window._active_skip_chapter=active
    if active:
        window._skip_button.set_label(f"Skip {active.get('label','Section')} →"); window._skip_button.set_visible(True)
    else:
        window._skip_button.set_visible(False)

    has_next=bool(session.get("next_episode"))
    credits=next((c for c in session.get("chapters",[]) if c.get("type")=="ed"),None)
    in_credits=False
    if credits:
        try: in_credits=pos>=float(credits["start"])
        except Exception: pass
    remaining=duration-pos if duration>0 else 9999
    show_next=has_next and (in_credits or remaining<=30)
    if show_next:
        if AUTOPLAY_DEFAULT and not window._autoplay_cancelled and 0<remaining<=10:
            seconds=max(1,int(remaining+0.999))
            window._next_overlay_button.set_label(f"Next Episode in {seconds}")
            window._cancel_autoplay_button.set_visible(True)
        else:
            window._next_overlay_button.set_label("Next Episode →")
            window._cancel_autoplay_button.set_visible(False)
        window._next_overlay_button.set_visible(True)
    elif not getattr(window,"_media_busy",False):
        window._next_overlay_button.set_visible(False); window._cancel_autoplay_button.set_visible(False)

def _install_session_ui(window):
    window._media_session={}
    window._media_busy=False
    window._session_refreshing=False
    window._progress_posting=False
    window._initial_resume_scheduled=False
    window._initial_resume_pending=False
    window._active_skip_chapter=None
    window._autoplay_cancelled=False
    window._subtitle_cues=[]
    window._subtitle_starts=[]
    window._subtitle_last_index=-2
    window._custom_subtitle=None
    window._custom_subtitle_active=False

    subtitle_label=Gtk.Label()
    subtitle_label.set_halign(Gtk.Align.CENTER)
    subtitle_label.set_valign(Gtk.Align.END)
    subtitle_label.set_justify(Gtk.Justification.CENTER)
    subtitle_label.set_wrap(True)
    subtitle_label.set_max_width_chars(72)
    subtitle_label.set_margin_start(36)
    subtitle_label.set_margin_end(36)
    subtitle_label.set_margin_bottom(78)
    subtitle_label.set_can_target(False)
    subtitle_label.add_css_class("osd")
    subtitle_label.add_css_class("title-3")
    subtitle_label.set_visible(False)
    video_overlay=window.picture.get_parent().get_parent()
    video_overlay.add_overlay(subtitle_label)
    window._subtitle_label=subtitle_label

    prev=Gtk.Button(icon_name="media-skip-backward-symbolic",tooltip_text="Previous Episode")
    next_=Gtk.Button(icon_name="media-skip-forward-symbolic",tooltip_text="Next Episode")
    for button in (prev,next_):
        button.add_css_class("circular"); button.add_css_class("highlighted"); button.add_css_class("overlaid")
        button.set_valign(Gtk.Align.CENTER)
    prev.connect("clicked",lambda *_:_navigate(window,"previous"))
    next_.connect("clicked",lambda *_:_navigate(window,"next"))
    window.controls_box.prepend(prev); window.controls_box.append(next_)
    window._episode_prev_button=prev; window._episode_next_button=next_

    actions=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=8)
    actions.set_halign(Gtk.Align.END); actions.set_margin_end(12); actions.set_margin_bottom(6)

    skip=Gtk.Button(label="Skip Intro →")
    skip.add_css_class("pill"); skip.add_css_class("highlighted"); skip.add_css_class("overlaid")
    skip.set_visible(False); skip.connect("clicked",lambda *_:_skip_current_chapter(window))

    cancel=Gtk.Button(label="Cancel")
    cancel.add_css_class("pill"); cancel.add_css_class("overlaid")
    cancel.set_visible(False); cancel.connect("clicked",lambda *_:_cancel_autoplay(window))

    next_overlay=Gtk.Button(label="Next Episode →")
    next_overlay.add_css_class("pill"); next_overlay.add_css_class("highlighted"); next_overlay.add_css_class("overlaid")
    next_overlay.set_visible(False); next_overlay.connect("clicked",lambda *_:_navigate(window,"next"))

    actions.append(skip); actions.append(cancel); actions.append(next_overlay)
    window.bottom_overlay_box.prepend(actions)
    window._skip_button=skip; window._cancel_autoplay_button=cancel; window._next_overlay_button=next_overlay

    subtitle_action=Gio.SimpleAction.new_stateful(
        "media-subtitle",GLib.VariantType.new("s"),GLib.Variant.new_string("off")
    )
    subtitle_action.connect("activate",lambda action,param:_subtitle_action(window,action,param))
    window.add_action(subtitle_action)

    _refresh_session_async(window)
    GLib.timeout_add_seconds(1,_poll_session,window)
    GLib.timeout_add_seconds(2,_post_progress,window)

def _premium_on_choose_subtitles(self,dialog,res):
    try:
        gfile=dialog.open_finish(res)
    except GLib.Error:
        return
    if not gfile:
        return
    try:
        loaded=gfile.load_contents(None)
        data=next((item for item in loaded if isinstance(item,(bytes,bytearray))),None)
        if data is None:
            raise RuntimeError('could not read subtitle file')
        name=gfile.get_basename() or 'Subtitle'
        cues=_parse_subtitle_file(bytes(data),name)
        if not cues:
            raise RuntimeError('no supported subtitle cues found')
        self._custom_subtitle={'name':name,'cues':cues}
        self._custom_subtitle_active=True
        _apply_subtitle_cues(self,cues)
        action=self.lookup_action('media-subtitle')
        if action:
            action.set_state(GLib.Variant.new_string('custom-local'))
        _rebuild_subtitle_menu(self)
        try:
            self.options.popover.popdown()
        except Exception:
            pass
        _toast(self,f'Loaded {name}')
    except Exception as exc:
        _toast(self,f'Could not load subtitles: {exc}')


def _premium_init(self,**kwargs):
    _original_init(self,**kwargs)
    if SESSION_URL: _install_session_ui(self)

def _premium_play_video(self,gfile):
    subtitle_uri=INITIAL_SUBTITLE_URI
    session=getattr(self,"_media_session",{}) or {}
    if session.get("subtitle_url"): subtitle_uri=str(session["subtitle_url"])
    self._media_stream_ready=False
    self._pending_subtitle_uri=str(subtitle_uri or "")
    self._pending_subtitle_attached=False
    try:
        self.play.set_subtitle_track_enabled(False)
        self.play.props.suburi=None
    except Exception:
        pass
    _original_play_video(self,gfile)
    if subtitle_uri:
        _set_external_subtitle(self,subtitle_uri)

def _premium_media_info_updated(self,obj,media_info):
    _original_media_info_updated(self,obj,media_info)

def _premium_position_updated(self,obj,pos):
    _original_position_updated(self,obj,pos)
    _update_subtitle_overlay(self,pos)
    if SESSION_URL: _update_timed_ui(self,pos)

def _premium_end_of_stream(self,obj):
    session=getattr(self,"_media_session",{}) or {}
    if SESSION_URL and AUTOPLAY_DEFAULT and not getattr(self,"_autoplay_cancelled",False) and session.get("next_episode") and not getattr(self,"_media_busy",False):
        _navigate(self,"next"); return
    _original_end_of_stream(self,obj)

def _premium_build_menus(self,media_info):
    _original_build_menus(self,media_info)
    root=self.get_root()
    if SESSION_URL and isinstance(root,Window): _rebuild_subtitle_menu(root)

Window.__init__=_premium_init
Window.play_video=_premium_play_video
Window._on_position_updated=_premium_position_updated
Window._on_media_info_updated=_premium_media_info_updated
Window._on_end_of_stream=_premium_end_of_stream
Window._on_choose_subtitles=_premium_on_choose_subtitles
Options.build_menus=_premium_build_menus

from showtime import main
raise SystemExit(main.main())
