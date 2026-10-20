#!/usr/bin/python3
"""GNOME Showtime launcher extended for ani-cli episode sessions."""
from __future__ import annotations
import bisect, gettext, html, json, locale, os, re, signal, sys, threading, urllib.parse, urllib.request, uuid, weakref
from pathlib import Path
from platform import system

BACKEND=os.environ.get("SHOWTIME_BACKEND", "gstreamer")
if BACKEND not in ("gstreamer", "libmpv"):
    raise SystemExit("Unsupported SHOWTIME_BACKEND")
PKG_DATA_DIR=os.environ.get("SHOWTIME_DATA_DIR", "/app/share/showtime")
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
gi.require_version("Gst","1.0")
gi.require_version("GstPlay","1.0")
from gi.repository import Adw,Gio,GLib,Gtk,Pango,Gst,GstPlay
GLib.set_prgname("showtime"); GLib.set_application_name(_("Video Player"))
Gtk.Window.set_default_icon_name("org.gnome.Showtime")
Gio.resources_register(Gio.Resource.load(str(Path(PKG_DATA_DIR,"showtime.gresource"))))
from showtime.play import Messenger
from showtime.widgets.options import Options
from showtime.widgets.window import Window
import showtime.widgets.window as _window_module

_original_init=Window.__init__
_original_play_video=Window.play_video
_original_position_updated=Window._on_position_updated
_original_media_info_updated=Window._on_media_info_updated
_original_end_of_stream=Window._on_end_of_stream
_original_on_error=Window._on_error
_original_playback_state_changed=Window._on_playback_state_changed
_original_build_menus=Options.build_menus

# Messenger drops the URI when translating GstPlay messages to UI signals.
# Filter BEFORE that translation: delayed old errors, EOS and positions must
# never acquire the new session's media_revision or affect its UI.
_SESSION_WINDOWS = weakref.WeakValueDictionary()
_original_play_bus_message = Messenger._on_play_bus_message


def _generation_uri(uri):
    # URI fragments are local identity only: HTTP never sends them to a CDN.
    # Gst file sources also ignore the fragment when opening the file.
    parts = urllib.parse.urlsplit(uri)
    if parts.scheme:
        return urllib.parse.urlunsplit(parts._replace(fragment="kunai-" + uuid.uuid4().hex))
    return uri


def _report_decoder_event(window, endpoint, reason=None):
    session = dict(getattr(window, "_media_session", {}) or {})
    if not SESSION_BASE or session.get("media_revision") is None:
        return
    payload = {"media_revision": session["media_revision"]}
    if reason:
        payload["reason"] = reason
    def worker():
        try:
            _http_json(f"{SESSION_BASE}/api/{endpoint}", method="POST", payload=payload, timeout=4)
        except Exception as exc:
            print(f"MediaPlayer: decoder report failed: {type(exc).__name__}", file=sys.stderr)
    threading.Thread(target=worker, daemon=True).start()


def _guarded_play_bus_message(messenger, bus, message):
    window = _SESSION_WINDOWS.get(bus)
    if window is None or not SESSION_URL:
        return _original_play_bus_message(messenger, bus, message)
    expected = getattr(window, "_decoder_uri", None)
    try:
        uri = GstPlay.play_message_get_uri(message)
        kind = GstPlay.play_message_parse_type(message)
    except Exception:
        return  # Older GstPlay without identity support cannot prove readiness.
    if not expected or uri != expected:
        return
    if kind == GstPlay.PlayMessage.STATE_CHANGED:
        state = GstPlay.play_message_parse_state_changed(message)
        window._decoder_state = state.value_nick.lower()
        window._decoder_pause_revision = (getattr(window,"_media_session",{}) or {}).get("pause_revision",0)
    elif kind == GstPlay.PlayMessage.MEDIA_INFO_UPDATED:
        info = GstPlay.play_message_parse_media_info_updated(message)
        window._decoder_video_streams = info.get_number_of_video_streams()
        window._decoder_audio_streams = info.get_number_of_audio_streams()
        window._decoder_seekable = info.is_seekable()
    elif kind == GstPlay.PlayMessage.POSITION_UPDATED:
        window._decoder_position = GstPlay.play_message_parse_position_updated(message)
    elif kind == GstPlay.PlayMessage.DURATION_CHANGED:
        window._decoder_duration = GstPlay.play_message_parse_duration_changed(message)
    elif kind == GstPlay.PlayMessage.SEEK_DONE:
        window._decoder_position = GstPlay.play_message_parse_seek_done(message)
        window._initial_resume_pending = False
    elif kind == GstPlay.PlayMessage.ERROR:
        error, _details = GstPlay.play_message_parse_error(message)
        window._decoder_state = "failed"
        http_status = _gst_http_status(_details)
        if os.environ.get("KUNAI_SHOWTIME_TRACE") == "1":
            # GStreamer details include signed URLs. Report only status, never
            # the remote URI, headers, or raw exception message.
            print(f"MediaPlayer trace: playback HTTP status={http_status or 'unknown'}",
                  file=sys.stderr, flush=True)
        denied = http_status in (401, 403, 429) or _is_source_access_denied(error.message)
        _report_decoder_event(window, "playback-error",
                              "access-denied" if denied else "playback-failed")
    elif kind == GstPlay.PlayMessage.END_OF_STREAM:
        _report_decoder_event(window, "playback-eof")
    result = _original_play_bus_message(messenger, bus, message)
    if kind == GstPlay.PlayMessage.POSITION_UPDATED:
        _post_progress(window)
    return result


Messenger._on_play_bus_message = _guarded_play_bus_message

def _http_json(url,method="GET",payload=None,timeout=45):
    data=None; headers={"Accept":"application/json"}
    if payload is not None:
        data=json.dumps(payload).encode(); headers["Content-Type"]="application/json"
    token=os.environ.get("MEDIA_PLAYER_SESSION_TOKEN","")
    if token and SESSION_BASE and url.startswith(SESSION_BASE + "/api/"):
        headers["X-Showtime-Session-Token"]=token
    req=urllib.request.Request(url,data=data,headers=headers,method=method)
    open_url=urllib.request.urlopen
    if "X-Showtime-Session-Token" in headers:
        class NoSessionRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None
        open_url=urllib.request.build_opener(NoSessionRedirect()).open
    with open_url(req,timeout=timeout) as response:
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
    """Use native subtitle decoding for mpv, or the existing GTK VTT overlay."""
    _clear_study_overlay(window)
    if BACKEND == "libmpv":
        _apply_subtitle_cues(window, [])
        window.play.fetch_subtitle(str(uri) if uri else None)
        if not uri: window.play.set_subtitle_track_enabled(False)
        return
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
        print(f"MediaPlayer: subtitle overlay load failed: {type(exc).__name__}",file=sys.stderr)

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

def _clear_study_overlay(window):
    window._study_active=False
    window._study_cues=[]
    window._study_starts=[]
    window._study_last_index=-2
    if hasattr(window,'_study_box'):
        window._study_box.set_visible(False)


def _apply_study_payload(window,payload):
    cues=[]
    for cue in payload.get('cues',[]):
        try:
            start=int(cue.get('start_ns')); end=int(cue.get('end_ns'))
        except Exception:
            continue
        if end<=start: continue
        cues.append(dict(cue,start_ns=start,end_ns=end))
    cues.sort(key=lambda x:x['start_ns'])
    window._study_cues=cues
    window._study_starts=[x['start_ns'] for x in cues]
    window._study_last_index=-2
    window._study_active=True
    if BACKEND == 'libmpv':
        window.play.fetch_subtitle(None)
        window.play.set_subtitle_track_enabled(False)
    window._study_meta={
        'japanese_source':str(payload.get('japanese_source') or 'Japanese'),
        'natural_source':str(payload.get('natural_source') or 'Original subtitle'),
        'gloss_language':str(payload.get('gloss_language') or 'English (JMdict)'),
    }
    _apply_subtitle_cues(window,[])


def _set_study_subtitle(window,uri):
    try:
        with urllib.request.urlopen(str(uri),timeout=8) as response:
            payload=json.loads(response.read().decode('utf-8','replace'))
        if not isinstance(payload,dict) or not isinstance(payload.get('cues'),list):
            raise RuntimeError('study endpoint returned invalid data')
        _apply_study_payload(window,payload)
    except Exception as exc:
        _clear_study_overlay(window)
        _toast(window,f'Could not load study subtitles: {type(exc).__name__}')


def _label_markup(label,text,size='small',weight=None,dim=False):
    escaped=html.escape(str(text or ''))
    attrs=[f'size="{size}"']
    if weight: attrs.append(f'weight="{weight}"')
    if dim: attrs.append('alpha="75%"')
    label.set_markup(f'<span {" ".join(attrs)}>{escaped}</span>')


def _compact_study_gloss(value):
    value=str(value or '').strip()
    if not value:
        return ''
    parts=[part.strip() for part in value.split(' · ') if part.strip()]
    lexical=parts[0] if parts else ''
    grammar=parts[1:] if len(parts)>1 else []
    lexical=lexical.split(' / ',1)[0].strip()
    lexical=re.sub(r'^to\s+','',lexical,flags=re.I)
    replacements={
        'be aware of':'know','be acquainted with':'know','name':'name',
        'one grade':'first-class','medium grade':'middle','sense of duty':'duty',
    }
    lexical=replacements.get(lexical.lower(),lexical)
    grammar_compact=[]
    for item in grammar:
        item=item.split(' / ',1)[0].strip()
        item=re.sub(r'^be\s+','',item,flags=re.I)
        if item and item not in grammar_compact:
            grammar_compact.append(item)
    result=lexical
    if grammar_compact:
        result=(result+' · '+','.join(grammar_compact)).strip(' ·')
    return result


def _study_aid_text(tokens):
    pieces=[]
    for token in tokens:
        if str(token.get('pos') or '')=='punct':
            continue
        gloss=_compact_study_gloss(token.get('gloss'))
        romaji=str(token.get('romaji') or '').strip()
        if gloss and romaji:
            pieces.append(f'{gloss} · {romaji}')
        elif gloss or romaji:
            pieces.append(gloss or romaji)
    return '    '.join(pieces)


def _contains_kanji(text):
    return any(
        "㐀"<=ch<="䶿" or "一"<=ch<="鿿" or "豈"<=ch<="﫿"
        for ch in str(text or "")
    )


def _render_study_cue(window,cue):
    flow=window._study_tokens
    child=flow.get_first_child()
    while child is not None:
        nxt=child.get_next_sibling()
        flow.remove(child)
        child=nxt

    tokens=list(cue.get('tokens',[]) or [])
    aid_text=_study_aid_text(tokens)
    aid_len=len(aid_text)
    aid_size='x-small' if aid_len<=90 else 'xx-small'
    _label_markup(window._study_aid,aid_text,aid_size,None,True)
    window._study_aid.set_tooltip_text('  |  '.join(
        f"{str(t.get('surface') or '')}: {str(t.get('gloss') or '')} · {str(t.get('romaji') or '')}".strip(' ·')
        for t in tokens if str(t.get('pos') or '')!='punct'
    ))

    jp_len=len(str(cue.get('japanese') or ''))
    base_size='x-large' if jp_len<=28 else 'large' if jp_len<=42 else 'medium'
    for token in tokens:
        surface=str(token.get('surface') or '')
        if not surface: continue
        surface_row=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=0)
        surface_row.set_halign(Gtk.Align.CENTER); surface_row.set_can_target(False)

        ruby=token.get('ruby') or [{'text':surface}]
        for segment in ruby:
            segment_text=str(segment.get('text') or '')
            if not segment_text: continue
            segment_box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=0)
            segment_box.set_halign(Gtk.Align.CENTER); segment_box.set_can_target(False)
            ruby_label=Gtk.Label(); ruby_label.set_halign(Gtk.Align.CENTER); ruby_label.set_can_target(False)
            base_label=Gtk.Label(); base_label.set_halign(Gtk.Align.CENTER); base_label.set_can_target(False)
            furigana=str(segment.get('furigana') or '')
            _label_markup(ruby_label,furigana if furigana else ' ','xx-small',None,True)
            _label_markup(base_label,segment_text,base_size,'bold')
            segment_box.append(ruby_label); segment_box.append(base_label)
            surface_row.append(segment_box)
        flow.append(surface_row)

    natural=' '.join(str(cue.get('natural') or '').splitlines()).strip()
    natural_len=len(natural)
    natural_size='large' if natural_len<=54 else 'medium' if natural_len<=72 else 'small' if natural_len<=90 else 'x-small'
    _label_markup(window._study_natural,natural,natural_size,'semibold')
    meta=getattr(window,'_study_meta',{}) or {}
    window._study_box.set_tooltip_text(
        f"JP: {meta.get('japanese_source','Japanese')}  •  Natural: {meta.get('natural_source','Original subtitle')}  •  Gloss: {meta.get('gloss_language','English (JMdict)')}"
    )
    window._study_box.set_visible(True)


def _update_study_overlay(window,pos_ns):
    if not getattr(window,'_study_active',False):
        if hasattr(window,'_study_box'): window._study_box.set_visible(False)
        return False
    cues=getattr(window,'_study_cues',[]) or []
    starts=getattr(window,'_study_starts',[]) or []
    if not cues or not starts:
        window._study_box.set_visible(False); return True
    pos=int(pos_ns)
    index=bisect.bisect_right(starts,pos)-1
    if index<0 or index>=len(cues) or not (cues[index]['start_ns']<=pos<cues[index]['end_ns']):
        index=-1
    if index==getattr(window,'_study_last_index',-2): return True
    window._study_last_index=index
    if index<0:
        window._study_box.set_visible(False)
    else:
        _render_study_cue(window,cues[index])
    return True


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
    study=Gio.Menu()
    study_more=Gio.Menu()
    generated=Gio.Menu()
    generated_more=Gio.Menu()
    common_generated={"generated-en","generated-de","generated-ja","generated-ja-romaji"}
    for track in session.get("subtitles",[]):
        track_id=str(track.get("id") or "")
        if not track_id:
            continue
        label=str(track.get("label") or track_id)
        if track.get("source") in ("generated","study") and not track.get("available"):
            status=str(track.get("status") or "idle")
            if status in ("starting","extracting","transcribing","translating","generating","preparing-japanese","analyzing","transcribing-quality"):
                current=track.get("current")
                total=track.get("total")
                if isinstance(current,int) and isinstance(total,int) and total>0:
                    phase="Japanese" if status=="transcribing-quality" else status.title()
                    label += f" · {phase} {current}/{total}"
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
        elif track.get("source")=="study":
            if str(track.get("language") or "").lower() in ("english","german"):
                study.append_item(item)
            else:
                study_more.append_item(item)
        else:
            original.append_item(item)

    if generated_more.get_n_items():
        generated.append_submenu("More AI Languages",generated_more)
    if original.get_n_items():
        menu.append_section("Original",original)
    if study_more.get_n_items():
        study.append_submenu("More Study Languages",study_more)
    if study.get_n_items():
        menu.append_section("Japanese Study",study)
    if generated.get_n_items():
        menu.append_section("AI Generated",generated)
    custom=getattr(window,"_custom_subtitle",None)
    if custom:
        custom_menu=Gio.Menu()
        custom_menu.append_item(_menu_item(f"Custom · {custom['name']}","win.media-subtitle","custom-local"))
        menu.append_section("Custom",custom_menu)
    if BACKEND == "libmpv":
        info = window.play.get_media_info()
        if info:
            embedded = Gio.Menu()
            for index, track in enumerate(info.get_subtitle_streams()):
                embedded.append(track.get_language() or f"Track {index + 1}",
                                f"win.select-subtitles(uint16 {index})")
            if embedded.get_n_items(): menu.append_section("Embedded", embedded)
    menu.append("Add Subtitle File…","win.choose-subtitles")


def _session_display_title(session):
    anime_title=str((session or {}).get("anime_title") or "").strip()
    episode=str((session or {}).get("episode") or "").strip()
    return f"{anime_title} · Episode {episode}" if anime_title and episode else anime_title


def _apply_session_title(window,session=None):
    display_title=_session_display_title(session if session is not None else getattr(window,"_media_session",{}))
    if not display_title:
        return
    window._session_display_title=display_title
    if BACKEND == "libmpv":
        info = window.play.get_media_info()
        if info and info.get_title() != display_title:
            info.title = display_title
            window.emit("media-info-updated")
    try:
        if window.title_label.props.label!=display_title:
            window.title_label.props.label=display_title
    except Exception: pass
    try:
        if window.get_title()!=display_title:
            window.set_title(display_title)
    except Exception: pass


def _session_needs_media_reload(previous,session):
    """True only for a new media load, not a title, seek or subtitle change.

    Kunai can reload the SAME URL after a 403/network error. An ordinary
    session revision cannot identify that, so use bridge media_revision.
    Legacy ani-cli session APIs have no media_revision: for those, a URL
    change still indicates that Showtime must actually open the new file.
    """
    if not previous:
        return False
    old_media=previous.get("media_url")
    new_media=session.get("media_url")
    if not old_media or not new_media:
        return False
    new_revision=session.get("media_revision")
    old_revision=previous.get("media_revision")
    if new_revision is not None and old_revision is not None:
        if new_revision != old_revision:
            return True
    return new_media != old_media


def _set_session(window,session,refresh_menu=True):
    previous=getattr(window,"_media_session",{}) or {}
    if (session.get("media_revision") is not None and previous.get("media_revision") is not None
            and session["media_revision"] < previous["media_revision"]):
        return
    if (session.get("media_revision") == previous.get("media_revision")
            and session.get("revision", 0) < previous.get("revision", 0)):
        return
    if "pause" in session and session.get("pause") != previous.get("pause"):
        if session["pause"]:
            window.pause()
        else:
            window.unpause()
    if os.environ.get("KUNAI_SHOWTIME_TRACE") == "1":
        print(f"MediaPlayer trace: poll old_media_rev={previous.get('media_revision')} new_media_rev={session.get('media_revision')} old_rev={previous.get('revision')} new_rev={session.get('revision')}",file=sys.stderr,flush=True)
    if _session_needs_media_reload(previous,session):
        if os.environ.get("KUNAI_SHOWTIME_TRACE") == "1":
            print("MediaPlayer trace: actually reloading media",file=sys.stderr,flush=True)
        # Mark this media generation as current BEFORE _play_session, since
        # _play_session calls _set_session to update all the normal UI fields.
        # Otherwise those two functions recursively call each other forever.
        window._media_session=session
        _play_session(window,session)
        return
    if session.get("playback_terminal") and not previous.get("playback_terminal"):
        window.play.stop()
    old_episode=str(previous.get("episode") or "")
    old_selected=str(previous.get("selected_subtitle_id") or "")
    window._media_session=session
    new_episode=str(session.get("episode") or "")
    _apply_session_title(window,session)
    new_selected=str(session.get("selected_subtitle_id") or "off")

    # A seek received during an episode transition must wait until the target
    # media is loaded. The old timeline may be longer than the new episode.
    seek_revision=int(session.get("seek_revision") or 0)
    if seek_revision and seek_revision!=getattr(window,"_external_seek_revision",0):
        window._external_seek_revision=seek_revision
        seek_us=max(0,int(session.get("seek_us") or 0))
        _schedule_media_seek(window,seek_us*1000,session.get("media_revision"))

    if new_episode!=old_episode:
        window._autoplay_cancelled=False
        window._custom_subtitle_active=False
        window._custom_subtitle=None
        if new_selected.startswith("study-ja-") and session.get("study_url"):
            _set_study_subtitle(window,session.get("study_url"))
        elif session.get("subtitle_url"):
            _set_external_subtitle(window,session.get("subtitle_url"))
        else:
            _set_external_subtitle(window,None)
    elif old_selected and new_selected!=old_selected and not getattr(window,"_custom_subtitle_active",False):
        if new_selected.startswith("study-ja-") and session.get("study_url"):
            _set_study_subtitle(window,session.get("study_url"))
        else:
            _set_external_subtitle(window,session.get("subtitle_url"))

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
            session=None; print(f"MediaPlayer: session refresh failed: {type(exc).__name__}",file=sys.stderr)
        def finish():
            window._session_refreshing=False
            if getattr(window.play, "closed", False): return GLib.SOURCE_REMOVE
            if session is not None:
                window._session_consecutive_failures=0
                # The window launches without media arguments: never expose
                # signed source URLs or bearer tokens in process argv.
                # The first authenticated session supplies the URI to open.
                if not getattr(window, "_media_session", None) and not window.play.props.uri:
                    _play_session(window, session)
                else:
                    _set_session(window,session)
            elif os.environ.get("MEDIA_PLAYER_SESSION_TOKEN"):
                window._session_consecutive_failures=getattr(window,"_session_consecutive_failures",0)+1
                if window._session_consecutive_failures>=3:
                    app=window.get_application()
                    if app: app.quit()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(finish)
    threading.Thread(target=worker,daemon=True).start()

def _poll_session(window):
    if getattr(window.play, "closed", False): return GLib.SOURCE_REMOVE
    _refresh_session_async(window)
    return GLib.SOURCE_CONTINUE

def _post_progress(window):
    if getattr(window.play, "closed", False): return GLib.SOURCE_REMOVE
    if (
        not SESSION_BASE
        or getattr(window,"_progress_posting",False)
        or getattr(window,"_media_busy",False)
        or getattr(window,"_initial_resume_pending",False)
        or not getattr(window,"_media_stream_ready",False)
    ):
        return GLib.SOURCE_CONTINUE
    try:
        position_ns=int(getattr(window,"_decoder_position",0))
        duration_ns=int(getattr(window,"_decoder_duration",0))
        if duration_ns == Gst.CLOCK_TIME_NONE:
            duration_ns=0
        session=dict(getattr(window,"_media_session",{}) or {})
        decoder_state=getattr(window,"_decoder_state","loading")
        pause_revision=getattr(window,"_decoder_pause_revision",0)
        video_streams=getattr(window,"_decoder_video_streams",0)
        audio_streams=getattr(window,"_decoder_audio_streams",0)
        seekable=getattr(window,"_decoder_seekable",False)
    except Exception:
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
                    "media_revision":session.get("media_revision"),
                    "seek_revision":session.get("seek_revision", 0),
                    "state":decoder_state,
                    "pause_revision":pause_revision,
                    "video_streams":video_streams,
                    "audio_streams":audio_streams,
                    "seekable":seekable,
                    "backend":getattr(window,"_decoder_backend","gstreamer"),
                    "rendered_frames":getattr(window,"_decoder_rendered_frames",0),
                    "audio_ready":getattr(window,"_decoder_audio_ready",False),
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

def _safe_seek_target_ns(request_ns,duration_ns):
    """Discard stale/resume positions outside the *new* media's timeline."""
    try:
        request_ns=int(request_ns)
        duration_ns=max(0,int(duration_ns or 0))
    except (TypeError,ValueError):
        return None
    if request_ns<0:
        return None
    # An old 22:30 resume must NOT seek to the end of a 21:58 episode.
    if duration_ns and request_ns>=max(0,duration_ns-1_000_000_000):
        return None
    return request_ns


def _schedule_media_seek(window,target_ns,media_revision):
    """Apply a current generation's seek only once the new stream is seekable."""
    generation=getattr(window,"_media_generation",0)
    window._seek_request_serial=getattr(window,"_seek_request_serial",0)+1
    serial=window._seek_request_serial
    window._initial_resume_pending=True
    attempts=0

    def try_seek():
        nonlocal attempts
        current=getattr(window,"_media_session",{}) or {}
        if getattr(window.play, "closed", False): return GLib.SOURCE_REMOVE
        if (getattr(window,"_media_generation",0)!=generation
                or getattr(window,"_seek_request_serial",0)!=serial
                or current.get("media_revision")!=media_revision):
            return GLib.SOURCE_REMOVE
        attempts+=1
        if not getattr(window,"_media_stream_ready",False):
            if attempts<50:
                return GLib.SOURCE_CONTINUE
            window._initial_resume_pending=False
            return GLib.SOURCE_REMOVE
        if getattr(window,"_decoder_seekable",True) is False:
            window._initial_resume_pending=False
            return GLib.SOURCE_REMOVE
        duration_ns=int(getattr(window.play.props,"duration",0) or 0)
        if duration_ns == 18446744073709551615:  # Gst.CLOCK_TIME_NONE
            duration_ns=0
        target=_safe_seek_target_ns(target_ns,duration_ns)
        if target is None:
            window._initial_resume_pending=False
            return GLib.SOURCE_REMOVE
        if not duration_ns and not getattr(window,"_decoder_seekable",False):
            if attempts<40:
                return GLib.SOURCE_CONTINUE
            window._initial_resume_pending=False
            return GLib.SOURCE_REMOVE
        try:
            window.play.seek(target)
            # GstPlay.seek() queues an asynchronous operation. Resume progress
            # only on the current URI's SEEK_DONE, with a bounded fallback.
            def seek_timeout():
                if (getattr(window,"_media_generation",0)==generation
                        and getattr(window,"_seek_request_serial",0)==serial):
                    window._initial_resume_pending=False
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(6000,seek_timeout)
            return GLib.SOURCE_REMOVE
        except Exception:
            if attempts<50:
                return GLib.SOURCE_CONTINUE
            window._initial_resume_pending=False
            print("MediaPlayer: seek unavailable for current media",file=sys.stderr)
            return GLib.SOURCE_REMOVE
    GLib.timeout_add(120,try_seek)


def _play_session(window,session):
    try:
        audio = window.play.get_current_audio_track()
        if audio and audio.get_language():
            window._preferred_audio_language = audio.get_language()
    except Exception:
        pass
    window._media_stream_ready=False
    window._episode_autoplay_pending=True
    window._media_generation=getattr(window,"_media_generation",0)+1
    window._seek_request_serial=getattr(window,"_seek_request_serial",0)+1
    window._initial_resume_pending=False
    window._active_skip_chapter=None
    window._custom_subtitle_active=False
    window._custom_subtitle=None
    window._autoplay_cancelled=False
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
    _install_video_request_headers(window, session.get("http_headers"))
    window._decoder_position=0
    window._decoder_duration=0
    window._decoder_seekable=False
    window._decoder_state="loading"
    window._decoder_video_streams=0
    window._decoder_audio_streams=0
    window._decoder_uri=media_url if BACKEND == "libmpv" else _generation_uri(media_url)
    _original_play_video(window,Gio.File.new_for_uri(window._decoder_uri))
    # Gio canonicalizes file URIs and strips fragments; restore identity on
    # GstPlay itself before playing (filesrc ignores that fragment).
    if window.play.props.uri != window._decoder_uri:
        window.play.props.uri = window._decoder_uri
    if not session.get("pause", False):
        window.unpause()
    _set_external_subtitle(window,subtitle_uri)
    resume_us=max(0,int(session.get("resume_us") or 0))
    if resume_us > 0 and (BACKEND == "libmpv" or resume_us > 5_000_000):
        _schedule_media_seek(window,resume_us*1000,session.get("media_revision"))


def _navigate(window,direction):
    if not SESSION_BASE or getattr(window,"_media_busy",False): return
    started_media_revision=(getattr(window,"_media_session",{}) or {}).get("media_revision")
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
                # When a controller reply races its session poll, the next
                # episode may already be loaded. Refresh before ever presenting
                # a failure to the user; only show unrecovered errors.
                _refresh_session_async(window)
                def report_if_unrecovered():
                    current=(getattr(window,"_media_session",{}) or {})
                    if current.get("media_revision")!=started_media_revision:
                        return GLib.SOURCE_REMOVE
                    _toast(window,"Could not load the next episode. Try again.")
                    return GLib.SOURCE_REMOVE
                GLib.timeout_add(2000,report_if_unrecovered)
                return GLib.SOURCE_REMOVE
            try:
                _set_session(window,session)
            except Exception as exc:
                _toast(window,f"Could not switch episode: {type(exc).__name__}")
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
            if error: _toast(window,f"Subtitle generation could not start: {type(error).__name__}")
            elif result: _set_session(window,result)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(finish)
    threading.Thread(target=worker,daemon=True).start()

def _generate_study(window,track_id):
    if not SESSION_BASE: return
    _toast(window,"Preparing Japanese study subtitles…")
    def worker():
        try:
            result=_http_json(f"{SESSION_BASE}/api/study/generate",method="POST",payload={"id":track_id},timeout=8); error=None
        except Exception as exc:
            result=None; error=exc
        def finish():
            if error: _toast(window,f"Study subtitles could not start: {type(error).__name__}")
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
    if track_id.startswith("study-ja-") and (not track or not track.get("available")):
        _generate_study(window,track_id); return
    def worker():
        try:
            updated=_http_json(f"{SESSION_BASE}/api/subtitles/select",method="POST",payload={"id":track_id},timeout=8); error=None
        except Exception as exc:
            updated=None; error=exc
        def finish():
            if error:
                _toast(window,f"Could not change subtitles: {type(error).__name__}"); return GLib.SOURCE_REMOVE
            _set_session(window,updated)
            action.set_state(GLib.Variant.new_string(track_id))
            return GLib.SOURCE_REMOVE
        GLib.idle_add(finish)
    threading.Thread(target=worker,daemon=True).start()

def _skip_current_chapter(window):
    chapter=getattr(window,"_active_skip_chapter",None)
    if not chapter or not getattr(window,"_media_stream_ready",False):
        return
    try:
        duration_ns=int(getattr(window.play.props,"duration",0) or 0)
        target=_safe_seek_target_ns(int(float(chapter["end"])*1_000_000_000),duration_ns)
        if target is not None:
            window.play.seek(target)
    except Exception:
        # A stale chapter is not a playback error; never flash a seek toast.
        return

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
    window._study_active=False
    window._study_cues=[]
    window._study_starts=[]
    window._study_last_index=-2
    window._study_meta={}
    window._session_display_title=None
    window._external_seek_revision=0

    subtitle_label=Gtk.Label()
    subtitle_label.set_halign(Gtk.Align.CENTER)
    subtitle_label.set_valign(Gtk.Align.END)
    subtitle_label.set_justify(Gtk.Justification.CENTER)
    subtitle_label.set_wrap(True)
    subtitle_label.set_max_width_chars(90)
    subtitle_label.set_margin_start(28)
    subtitle_label.set_margin_end(28)
    subtitle_label.set_margin_bottom(62)
    subtitle_label.set_can_target(False)
    subtitle_label.add_css_class("osd")
    subtitle_label.add_css_class("title-3")
    subtitle_label.set_visible(False)
    video_overlay=window.picture.get_parent().get_parent()
    video_overlay.add_overlay(subtitle_label)
    window._subtitle_label=subtitle_label

    study_box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=0)
    study_box.set_halign(Gtk.Align.CENTER); study_box.set_valign(Gtk.Align.END)
    study_box.set_margin_start(28); study_box.set_margin_end(28); study_box.set_margin_bottom(62)
    study_box.set_can_target(False); study_box.add_css_class("osd"); study_box.set_visible(False)
    study_aid=Gtk.Label(); study_aid.set_halign(Gtk.Align.CENTER); study_aid.set_justify(Gtk.Justification.CENTER)
    study_aid.set_single_line_mode(True); study_aid.set_wrap(False); study_aid.set_ellipsize(Pango.EllipsizeMode.END)
    study_aid.set_max_width_chars(135); study_aid.set_can_target(False); study_aid.set_margin_bottom(1)
    study_tokens=Gtk.FlowBox()
    study_tokens.set_selection_mode(Gtk.SelectionMode.NONE)
    study_tokens.set_halign(Gtk.Align.CENTER); study_tokens.set_homogeneous(False)
    study_tokens.set_row_spacing(0); study_tokens.set_column_spacing(0)
    study_tokens.set_min_children_per_line(1); study_tokens.set_max_children_per_line(40)
    study_tokens.set_can_target(False)
    study_natural=Gtk.Label(); study_natural.set_halign(Gtk.Align.CENTER); study_natural.set_justify(Gtk.Justification.CENTER)
    study_natural.set_single_line_mode(True); study_natural.set_wrap(False); study_natural.set_ellipsize(Pango.EllipsizeMode.END)
    study_natural.set_max_width_chars(120); study_natural.set_can_target(False)
    study_natural.set_margin_top(0); study_natural.set_margin_bottom(0)
    study_box.append(study_aid); study_box.append(study_tokens); study_box.append(study_natural)
    video_overlay.add_overlay(study_box)
    window._study_box=study_box; window._study_aid=study_aid; window._study_tokens=study_tokens
    window._study_natural=study_natural

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
    if BACKEND == "libmpv":
        _clear_study_overlay(self)
        _apply_subtitle_cues(self, [])
        self._custom_subtitle_active = True
        self.play.fetch_subtitle(gfile.get_uri())
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
        _toast(self,f'Could not load subtitles: {type(exc).__name__}')


def _premium_init(self,**kwargs):
    _original_init(self,**kwargs)
    if BACKEND == "libmpv":
        from showtime_libmpv_gtk import bind_window
        bind_window(self)
    else:
        _SESSION_WINDOWS[self.play.get_message_bus()] = self
    if SESSION_URL: _install_session_ui(self)

def _install_video_request_headers(window, header_map):
    """Apply resolved stream headers to the video URI source, not subtitle loads.

    Showtime uses GstPlay/playbin3 and otherwise loses mpv's per-file headers.
    In GStreamer the souphttpsrc element exposes these as extra-headers, while
    urisourcebin creates the actual HTTP source deeper in its bin.
    """
    if BACKEND == "libmpv":
        from showtime_libmpv import validated_headers
        validated_headers(header_map)
        window.play.headers = dict(header_map or {})
        return
    window._media_header_generation = getattr(window, "_media_header_generation", 0) + 1
    generation = window._media_header_generation
    for obj, handler in getattr(window, "_media_header_handlers", []):
        try:
            if obj.handler_is_connected(handler):
                obj.disconnect(handler)
        except Exception:
            pass
    window._media_header_handlers = []
    if not isinstance(header_map, dict) or not header_map:
        return

    def configure_source(element):
        if window._media_header_generation != generation:
            return
        try:
            extra = Gst.Structure.new_empty("headers")
            has_extra = bool(element.find_property("extra-headers"))
            for name, value in header_map.items():
                if not isinstance(name, str) or not isinstance(value, str):
                    continue
                if name.lower() == "user-agent" and element.find_property("user-agent"):
                    element.set_property("user-agent", value)
                elif name.lower() == "cookie" and element.find_property("cookies"):
                    element.set_property("cookies", [value])
                elif has_extra:
                    extra.set_value(name, value)
            if has_extra and extra.n_fields() > 0:
                element.set_property("extra-headers", extra)
        except Exception as exc:
            print(f"MediaPlayer: could not configure media headers: {type(exc).__name__}", file=sys.stderr)

    def setup_cb(pipeline, source):
        if window._media_header_generation != generation:
            return
        try:
            pipeline.disconnect_by_func(setup_cb)
        except Exception:
            pass
        configure_source(source)
        # Some versions hand a urisourcebin to source-setup. Only observe this
        # specific video source's descendants; never attach to the whole
        # pipeline where subtitle requests could accidentally inherit cookies.
        if isinstance(source, Gst.Bin):
            handler = source.connect("deep-element-added", lambda _bin, _sub_bin, element: configure_source(element))
            window._media_header_handlers.append((source, handler))

    handler = window.pipeline.connect("source-setup", setup_cb)
    window._media_header_handlers.append((window.pipeline, handler))


def _premium_play_video(self,gfile):
    subtitle_uri=INITIAL_SUBTITLE_URI
    session=getattr(self,"_media_session",{}) or {}
    if not session and SESSION_URL:
        try: session=_http_json(SESSION_URL,timeout=2)
        except Exception: pass
    if session.get("subtitle_url"): subtitle_uri=str(session["subtitle_url"])
    _install_video_request_headers(self,session.get("http_headers"))
    self._media_stream_ready=False
    self._pending_subtitle_uri=str(subtitle_uri or "")
    self._pending_subtitle_attached=False
    try:
        self.play.set_subtitle_track_enabled(False)
        self.play.props.suburi=None
    except Exception:
        pass
    self._decoder_position=0
    self._decoder_duration=0
    self._decoder_seekable=False
    self._decoder_state="loading"
    self._decoder_video_streams=0
    self._decoder_audio_streams=0
    self._decoder_uri=gfile.get_uri() if BACKEND == "libmpv" else _generation_uri(gfile.get_uri())
    _original_play_video(self,Gio.File.new_for_uri(self._decoder_uri))
    if self.play.props.uri != self._decoder_uri:
        self.play.props.uri = self._decoder_uri
    if subtitle_uri:
        _set_external_subtitle(self,subtitle_uri)
    if SESSION_URL and not session.get("pause", False):
        # The subtitle and video setup may both pause the underlying pipeline.
        # Start the new video only after its subtitle source is attached.
        self.unpause()

def _premium_playback_state_changed(self,obj,state):
    if os.environ.get("KUNAI_SHOWTIME_TRACE")=="1":
        print(f"MediaPlayer trace: state-change {state}",file=sys.stderr,flush=True)
    result=_original_playback_state_changed(self,obj,state)
    if SESSION_URL:
        if state==GstPlay.PlayState.PLAYING:
            self._episode_autoplay_pending=False
        elif (state==GstPlay.PlayState.PAUSED
              and getattr(self,"_episode_autoplay_pending",False)
              and getattr(self,"_media_stream_ready",False)
              and not (getattr(self,"_media_session",{}) or {}).get("pause",False)):
            # GstPlay may pause after an earlier 'play' command while replacing
            # an existing URI. Reassert autoplay only once, after the new
            # media is ready; never override a later user-initiated pause.
            self._episode_autoplay_pending=False
            self.unpause()
    return result

def _premium_media_info_updated(self,obj,media_info):
    self._media_stream_ready=True
    preferred=getattr(self,"_preferred_audio_language",None)
    if preferred:
        for track in media_info.get_audio_streams():
            if track.get_language()==preferred:
                self.play.set_audio_track(track.get_index())
                self._preferred_audio_language=None
                break
    if os.environ.get("KUNAI_SHOWTIME_TRACE")=="1":
        print("MediaPlayer trace: media info updated",file=sys.stderr,flush=True)
    if not SESSION_URL:
        _original_media_info_updated(self,obj,media_info)
        return
    self.options.menus_building += 1
    GLib.timeout_add(500,self.options.build_menus,media_info)
    self.emit("media-info-updated")
    _apply_session_title(self)

def _premium_position_updated(self,obj,pos):
    _original_position_updated(self,obj,pos)
    if not _update_study_overlay(self,pos):
        _update_subtitle_overlay(self,pos)
    if SESSION_URL: _update_timed_ui(self,pos)

def _gst_http_status(details):
    # The GstPlay error message often says only "resource not found" while its
    # details carry the actual 401/403/429. That distinction must reach Kunai.
    try:
        if details and details.has_field("http-status-code"):
            code = int(details.get_value("http-status-code"))
            return code if 100 <= code <= 599 else None
    except (TypeError, ValueError, OverflowError, AttributeError):
        pass
    return None


def _is_source_access_denied(message):
    """Recognize upstream HTTP authorization denial, not codec failures."""
    return bool(re.search(
        r"(?:403|forbidden|not authorized to access|access denied|permission denied|unauthorized)",
        str(message or ""), re.I,
    ))


def _premium_on_error(self,obj,error):
    if not SESSION_BASE:
        _original_on_error(self,obj,error)
        return

    # The guarded GstPlay bus reports the current source's error exactly once.
    # Leave provider recovery to Kunai without exposing signed source URLs.
    return

def _premium_end_of_stream(self,obj):
    session=getattr(self,"_media_session",{}) or {}
    if SESSION_URL and AUTOPLAY_DEFAULT and not getattr(self,"_autoplay_cancelled",False) and session.get("next_episode") and not getattr(self,"_media_busy",False):
        _navigate(self,"next"); return
    _original_end_of_stream(self,obj)

def _premium_build_menus(self,media_info):
    _original_build_menus(self,media_info)
    root=self.get_root()
    if SESSION_URL and isinstance(root,Window): _rebuild_subtitle_menu(root)

if BACKEND == "libmpv":
    _native_original_subtitles_selected = Window._on_subtitles_selected
    def _native_subtitles_selected(self, action, state):
        _clear_study_overlay(self)
        _apply_subtitle_cues(self, [])
        self._custom_subtitle_active = True
        # Invalidate an in-flight controller subtitle download without removing
        # the embedded/native track whose index the user just selected.
        self.play.subtitle_serial += 1
        _native_original_subtitles_selected(self, action, state)
    Window._on_subtitles_selected = _native_subtitles_selected

Window.__init__=_premium_init
Window.play_video=_premium_play_video
Window._on_position_updated=_premium_position_updated
Window._on_media_info_updated=_premium_media_info_updated
Window._on_end_of_stream=_premium_end_of_stream
Window._on_error=_premium_on_error
Window._on_playback_state_changed=_premium_playback_state_changed
Window._on_choose_subtitles=_premium_on_choose_subtitles
Options.build_menus=_premium_build_menus

if BACKEND == "libmpv":
    from showtime_libmpv_gtk import install as _install_native
    _original_play_video = _install_native(_window_module, _report_decoder_event, _post_progress, bool(SESSION_URL))

from showtime import main
if BACKEND == "libmpv" and SESSION_URL:
    # Upstream Showtime leaves MPRIS Next/Previous empty for a single file.
    # Kunai owns episode resolution; use the same bridge as its GTK buttons.
    from showtime.mpris import MPRIS
    MPRIS._next = lambda self: _navigate(self.win, "next") if self.win else None
    MPRIS._previous = lambda self: _navigate(self.win, "previous") if self.win else None
# The real Showtime application is a D-Bus singleton. Without NON_UNIQUE,
# later Kunai invocations forward URLs to an old process with old session
# headers, dead bridge ports and outdated controllers.
_original_application_init = main.Application.__init__
def _kunai_application_init(self):
    _original_application_init(self)
    if SESSION_URL:
        self.set_flags(self.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
main.Application.__init__ = _kunai_application_init
raise SystemExit(main.main())
