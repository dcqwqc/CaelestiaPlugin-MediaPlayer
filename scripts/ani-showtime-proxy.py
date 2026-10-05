#!/usr/bin/env python3
import argparse, hashlib, html, json, math, os, re, subprocess, sys, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

GENERATED_LANGUAGES = [
    ('en','English'),
    ('de','German'),
    ('ja','Japanese'),
    ('ja-romaji','Japanese Romaji'),
    ('af','Afrikaans'),
    ('ar','Arabic'),
    ('eu','Basque'),
    ('bn','Bengali'),
    ('bg','Bulgarian'),
    ('ca','Catalan'),
    ('zh','Chinese (Simplified)'),
    ('zh-TW','Chinese (Traditional)'),
    ('hr','Croatian'),
    ('cs','Czech'),
    ('da','Danish'),
    ('nl','Dutch'),
    ('et','Estonian'),
    ('fil','Filipino'),
    ('fi','Finnish'),
    ('fr','French'),
    ('gl','Galician'),
    ('el','Greek'),
    ('gu','Gujarati'),
    ('he','Hebrew'),
    ('hi','Hindi'),
    ('hu','Hungarian'),
    ('is','Icelandic'),
    ('id','Indonesian'),
    ('it','Italian'),
    ('kn','Kannada'),
    ('ko','Korean'),
    ('lv','Latvian'),
    ('lt','Lithuanian'),
    ('ms','Malay'),
    ('ml','Malayalam'),
    ('mt','Maltese'),
    ('mr','Marathi'),
    ('no','Norwegian'),
    ('fa','Persian'),
    ('pl','Polish'),
    ('pt','Portuguese'),
    ('pa','Punjabi'),
    ('ro','Romanian'),
    ('ru','Russian'),
    ('sr','Serbian'),
    ('sk','Slovak'),
    ('sl','Slovenian'),
    ('es','Spanish'),
    ('sw','Swahili'),
    ('sv','Swedish'),
    ('ta','Tamil'),
    ('te','Telugu'),
    ('th','Thai'),
    ('tr','Turkish'),
    ('uk','Ukrainian'),
    ('ur','Urdu'),
    ('vi','Vietnamese'),
]

class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads=True

    def handle_error(self,request,client_address):
        exc=sys.exc_info()[1]
        if isinstance(exc,(BrokenPipeError,ConnectionResetError,ConnectionAbortedError)):
            return
        super().handle_error(request,client_address)


class Proxy:
    def __init__(self, video, subtitle, referrer, port=0):
        self.video=video; self.subtitle=subtitle; self.referrer=referrer; self.port=port
        self.root=Path(__file__).resolve().parent
        self.video_duration=None
        self.segment_durations=[]
        self._subtitle_cues=None
        self.cache_root=Path(os.environ.get('XDG_CACHE_HOME', str(Path.home()/'.cache'))) / 'media-player' / 'hls'
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.cache_max_bytes=int(float(os.environ.get('ANI_SHOWTIME_CACHE_GB','12')) * (1024**3))
        self.cache_lock=threading.Lock()
        self.prefetch=ThreadPoolExecutor(max_workers=int(os.environ.get('ANI_SHOWTIME_PREFETCH_WORKERS','8')))
        self.prefetched=set()
        self.session_lock=threading.RLock()
        self.revision=1
        self.prepared={}
        self.resolve_events={}
        self.ai_processes={}
        self.study_processes={}
        self.revisions={}
        self.chapters=[]
        self.chapter_status='idle'
        self.subtitle_cache=Path(os.environ.get('XDG_CACHE_HOME', str(Path.home()/'.cache'))) / 'media-player' / 'subtitles'
        self.subtitle_cache.mkdir(parents=True,exist_ok=True)
        self.anime_id=os.environ.get('ANI_SHOWTIME_ANIME_ID','')
        self.anime_title=os.environ.get('ANI_SHOWTIME_ANIME_TITLE','')
        self.episode=os.environ.get('ANI_SHOWTIME_EPISODE','')
        self.episode_list=[x for x in os.environ.get('ANI_SHOWTIME_EPISODE_LIST','').split() if x]
        self.mal_id=os.environ.get('ANI_SHOWTIME_MAL_ID','')
        self.mode=os.environ.get('ANI_SHOWTIME_MODE','sub')
        self.quality=os.environ.get('ANI_SHOWTIME_QUALITY','best')
        self.subtitle_tracks=self.parse_subtitle_catalog(os.environ.get('ANI_SHOWTIME_SUBTITLE_CATALOG',''),subtitle)
        self.selected_subtitle_id=next((x['id'] for x in self.subtitle_tracks if x.get('default')), self.subtitle_tracks[0]['id'] if self.subtitle_tracks else 'off')
        self.pending_subtitle_id=None
        self._store_revision()
        threading.Thread(target=self.prune_cache,daemon=True).start()
        threading.Thread(target=self.prefetch_adjacent,daemon=True).start()
    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.port}'

    @staticmethod
    def parse_subtitle_catalog(raw, fallback=''):
        tracks=[]
        try:
            items=json.loads(raw) if raw and raw.strip() else []
        except Exception:
            items=[]
        if isinstance(items,list):
            for item in items:
                if not isinstance(item,dict):
                    continue
                url=item.get('src') or item.get('file') or item.get('url')
                if not isinstance(url,str) or not url:
                    continue
                kind=str(item.get('kind','captions')).lower()
                if kind not in ('captions','subtitles','caption','subtitle',''):
                    continue
                label=str(item.get('label') or item.get('name') or item.get('lang') or item.get('language') or 'Unknown')
                tracks.append({
                    'id':f'original-{len(tracks)}',
                    'source':'original',
                    'label':f'Original AniCLI {label}',
                    'language':label,
                    'url':url,
                    'default':bool(item.get('default',False)),
                    'available':True,
                })
        if fallback and not tracks:
            tracks.append({
                'id':'original-0','source':'original','label':'Original AniCLI Default',
                'language':'Default','url':fallback,'default':True,'available':True,
            })
        return tracks

    def generated_path(self,lang,episode=None,anime_id=None):
        logical=f'{anime_id or self.anime_id}:{episode or self.episode}'
        key=hashlib.sha256(logical.encode()).hexdigest()[:24]
        return self.subtitle_cache/key/f'generated-{lang}.vtt'

    def generated_status(self,lang,episode=None,anime_id=None):
        path=self.generated_path(lang,episode,anime_id)
        if path.exists():
            return {'status':'ready','error':None}
        status_path=path.with_name(f'generated-{lang}.status.json')
        try:
            value=json.loads(status_path.read_text())
            if not isinstance(value,dict):
                return {'status':'idle','error':None}
            state=str(value.get('status') or 'idle')
            progress_file=None
            if state=='transcribing':
                progress_file=path.parent/'asr-backend.json'
            elif state=='translating':
                progress_file=path.parent/'translation-progress.json'
            if progress_file and progress_file.exists():
                try:
                    progress=json.loads(progress_file.read_text())
                    if isinstance(progress,dict):
                        if state=='transcribing':
                            current=progress.get('block',progress.get('chunk'))
                            total=progress.get('blocks',progress.get('chunks'))
                        else:
                            if progress.get('target') not in (None,lang):
                                current=total=None
                            else:
                                current=progress.get('current')
                                total=progress.get('total')
                        if isinstance(current,int):
                            value['current']=current
                        if isinstance(total,int):
                            value['total']=total
                        if progress.get('backend'):
                            value['backend']=progress.get('backend')
                        if progress.get('device'):
                            value['device']=progress.get('device')
                except Exception:
                    pass
            return value
        except Exception:
            return {'status':'idle','error':None}

    @staticmethod
    def _is_japanese_track(track):
        label=str(track.get('language') or track.get('label') or '').lower()
        return any(x in label for x in ('japanese','日本語',' ja ','[ja]')) or label.strip() in ('ja','jp')

    def _japanese_original(self,payload=None):
        tracks=(payload or {}).get('subtitle_tracks') if payload is not None else self.subtitle_tracks
        return next((x for x in (tracks or []) if self._is_japanese_track(x)),None)

    def study_path(self,natural_track_id,episode=None,anime_id=None):
        logical=f'{anime_id or self.anime_id}:{episode or self.episode}'
        key=hashlib.sha256(logical.encode()).hexdigest()[:24]
        safe=re.sub(r'[^A-Za-z0-9_.-]+','_',natural_track_id)
        return self.subtitle_cache/key/f'study-ja-{safe}.json'

    def study_status(self,natural_track_id,episode=None,anime_id=None):
        path=self.study_path(natural_track_id,episode,anime_id)
        if path.exists(): return {'status':'ready','error':None}
        status_path=path.with_suffix('.status.json')
        try:
            value=json.loads(status_path.read_text())
            return value if isinstance(value,dict) else {'status':'idle','error':None}
        except Exception:
            return {'status':'idle','error':None}

    def study_tracks(self):
        tracks=[]
        for track in self.subtitle_tracks:
            if self._is_japanese_track(track):
                continue
            track_id=str(track.get('id') or '')
            if not track_id: continue
            state=self.study_status(track_id)
            language=str(track.get('language') or 'Original')
            tracks.append({
                'id':f'study-ja-{track_id}','source':'study','natural_track_id':track_id,
                'label':f'Japanese Study · {language}','language':language,
                'available':self.study_path(track_id).exists(),
                'status':state.get('status','idle'),'error':state.get('error'),
                'japanese_source':'Original Japanese' if self._japanese_original() else 'Qwen3-ASR Japanese fallback',
            })
        return tracks

    def subtitle_catalog(self):
        tracks=[dict(x,url=None) for x in self.subtitle_tracks]
        for lang,label in GENERATED_LANGUAGES:
            path=self.generated_path(lang)
            state=self.generated_status(lang)
            tracks.append({
                'id':f'generated-{lang}','source':'generated',
                'label':f'AI Generated {label}','language':label,
                'available':path.exists(),'status':state.get('status','idle'),
                'error':state.get('error'),'url':None,
            })
        tracks.extend(self.study_tracks())
        return tracks

    def _snapshot(self):
        return {
            'video':self.video,
            'subtitle':self.subtitle,
            'referrer':self.referrer,
            'anime_id':self.anime_id,
            'anime_title':self.anime_title,
            'episode':self.episode,
            'episode_list':list(self.episode_list),
            'mal_id':self.mal_id,
            'mode':self.mode,
            'quality':self.quality,
            'subtitle_tracks':[dict(x) for x in self.subtitle_tracks],
        }

    def _store_revision(self):
        self.revisions[int(self.revision)]=self._snapshot()
        # Keep enough history for long-running AI jobs while bounding stale
        # signed URLs during a very long binge session.
        while len(self.revisions)>64:
            oldest=min(self.revisions)
            self.revisions.pop(oldest,None)

    def revision_payload(self,revision=None):
        if revision is None:
            revision=self.revision
        try:
            revision=int(revision)
        except (TypeError,ValueError):
            return None
        with self.session_lock:
            payload=self.revisions.get(revision)
            return dict(payload) if payload else None

    def adjacent(self,direction):
        try:
            idx=self.episode_list.index(str(self.episode))
        except ValueError:
            return None
        target=idx+direction
        return self.episode_list[target] if 0<=target<len(self.episode_list) else None

    def session_json(self):
        with self.session_lock:
            rev=self.revision
            selected=self.selected_subtitle_id
            is_study=selected.startswith('study-ja-')
            subtitle_url=None if selected=='off' or is_study else f'{self.base_url}/api/subtitles/{urllib.parse.quote(selected)}.vtt?rev={rev}'
            study_url=(f'{self.base_url}/api/study/{urllib.parse.quote(selected)}.json?rev={rev}' if is_study else None)
            return {
                'anime_id':self.anime_id,'anime_title':self.anime_title,'episode':self.episode,
                'previous_episode':self.adjacent(-1),'next_episode':self.adjacent(1),
                'mal_id':self.mal_id,'mode':self.mode,'quality':self.quality,'revision':rev,
                'media_url':f'{self.base_url}/master.m3u8?rev={rev}',
                'subtitle_url':subtitle_url,'study_url':study_url,'selected_subtitle_id':selected,
                'subtitles':self.subtitle_catalog(),'chapters':list(self.chapters),
                'prepared_episodes':sorted(self.prepared),'resume_us':self.resume_us(),
                'pending_subtitle_id':self.pending_subtitle_id,
            }

    def payload_from_dict(self,body):
        tracks=self.parse_subtitle_catalog(body.get('subtitle_catalog',''),str(body.get('subtitle') or ''))
        ep_list=body.get('episode_list') or []
        if isinstance(ep_list,str):
            ep_list=[x for x in ep_list.split() if x]
        return {
            'video':str(body.get('video') or ''),
            'subtitle':str(body.get('subtitle') or ''),
            'referrer':str(body.get('referrer') or self.referrer),
            'anime_id':str(body.get('anime_id') or self.anime_id),
            'anime_title':str(body.get('anime_title') or self.anime_title),
            'episode':str(body.get('episode') or ''),
            'episode_list':list(ep_list),
            'mal_id':str(body.get('mal_id') or self.mal_id),
            'mode':str(body.get('mode') or self.mode),
            'quality':str(body.get('quality') or self.quality),
            'subtitle_tracks':tracks,
        }

    def apply_payload(self,payload):
        with self.session_lock:
            self.video=payload['video']
            self.subtitle=payload.get('subtitle','')
            self.referrer=payload.get('referrer',self.referrer)
            self.anime_id=payload.get('anime_id',self.anime_id)
            self.anime_title=payload.get('anime_title',self.anime_title)
            self.episode=payload.get('episode',self.episode)
            self.episode_list=payload.get('episode_list') or self.episode_list
            self.mal_id=payload.get('mal_id',self.mal_id)
            self.mode=payload.get('mode',self.mode)
            self.quality=payload.get('quality',self.quality)
            self.subtitle_tracks=payload.get('subtitle_tracks') or []
            self.selected_subtitle_id=next(
                (x['id'] for x in self.subtitle_tracks if x.get('default')),
                self.subtitle_tracks[0]['id'] if self.subtitle_tracks else 'off',
            )
            self.pending_subtitle_id=None
            self.video_duration=None
            self.segment_durations=[]
            self._subtitle_cues=None
            self.chapters=[]
            self.chapter_status='idle'
            self.revision+=1
            self._store_revision()
            # Prepared payloads are tiny, but do not let a long binge accumulate
            # stale signed stream URLs. Keep only the immediate neighbors.
            allowed={x for x in (self.adjacent(-1),self.adjacent(1)) if x}
            self.prepared={ep:data for ep,data in self.prepared.items() if ep in allowed}
        threading.Thread(target=self.prefetch_adjacent,daemon=True).start()
        return self.session_json()

    def accept_handoff(self,body,mode='prefetch'):
        payload=self.payload_from_dict(body)
        episode=payload.get('episode','')
        if not payload.get('video') or not episode:
            raise RuntimeError('invalid handoff')
        with self.session_lock:
            self.prepared[episode]=payload
            event=self.resolve_events.get(episode)
            if event:
                event.set()
        if mode in ('prefetch','switch'):
            threading.Thread(target=self.warm_payload,args=(payload,None),daemon=True).start()
        elif mode=='preview':
            preview_limit=max(1,int(os.environ.get('ANI_SHOWTIME_PREVIOUS_PREFETCH_SEGMENTS','24')))
            threading.Thread(target=self.warm_payload,args=(payload,preview_limit),daemon=True).start()
        if mode=='switch':
            with self.session_lock:
                self.prepared.pop(episode,None)
            return self.apply_payload(payload)
        return {'ok':True,'prepared':episode}

    def warm_payload(self,payload,max_segments=None):
        try:
            with self.req(payload['video'],referrer=payload.get('referrer')) as resp:
                text=resp.read().decode('utf-8','replace')
            urls=[]
            for line in text.splitlines():
                line=line.strip()
                if line and not line.startswith('#'):
                    url=urllib.parse.urljoin(payload['video'],line)
                    if urllib.parse.urlparse(url).path.lower().endswith(('.ts','.jpg','.jpeg')):
                        urls.append(url)
            # The caller controls how aggressively an adjacent episode warms:
            # next = full VOD; previous = a small instant-start window.
            if max_segments is None:
                configured=int(os.environ.get('ANI_SHOWTIME_NEXT_PREFETCH_SEGMENTS','0'))
                targets=urls if configured<=0 else urls[:configured]
            else:
                targets=urls[:max_segments]
            for url in targets:
                self.prefetch_url(url,payload.get('referrer'))
        except Exception:
            pass

    def resolve_episode(self,episode,timeout=35,mode='prefetch'):
        episode=str(episode)
        with self.session_lock:
            if episode in self.prepared:
                return True
            if episode in self.resolve_events:
                event=self.resolve_events[episode]
            else:
                event=self.resolve_events[episode]=threading.Event()
                env=os.environ.copy()
                env.update({
                    'ANI_SHOWTIME_FORCE_ANIME_ID':self.anime_id,
                    'ANI_SHOWTIME_HANDOFF_URL':f'{self.base_url}/api/handoff?mode={urllib.parse.quote(mode)}',
                    'ANI_SHOWTIME_FULLSCREEN':'0',
                    'TERM':'dumb',
                })
                logdir=Path(os.environ.get('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}'))/'ani-showtime'
                logdir.mkdir(parents=True,exist_ok=True)
                log=(logdir/f'prefetch-{episode}.log').open('ab')
                cmd=[str(Path.home()/'.local/bin/ani-cli'),'-e',episode,self.anime_title or self.anime_id]
                subprocess.Popen(cmd,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        ok=event.wait(timeout)
        with self.session_lock:
            self.resolve_events.pop(episode,None)
        return ok and episode in self.prepared

    def switch_episode(self,episode):
        episode=str(episode)
        if not self.resolve_episode(episode):
            raise RuntimeError(f'could not resolve episode {episode}')
        with self.session_lock:
            payload=self.prepared.pop(episode)
        return self.apply_payload(payload)

    def navigate(self,direction):
        target=self.adjacent(direction)
        if not target:
            raise RuntimeError('no adjacent episode')
        return self.switch_episode(target)

    def prefetch_adjacent(self):
        time.sleep(.35)
        # Resolve both directions so navigation is immediate, but spend the
        # bandwidth asymmetrically: next is fully cached for autoplay, previous
        # only gets a short instant-start window until it becomes current.
        plans=((1,'prefetch'),(-1,'preview'))
        for direction,mode in plans:
            target=self.adjacent(direction)
            if not target:
                continue
            try:
                self.resolve_episode(target,timeout=35,mode=mode)
            except Exception:
                pass

    def ensure_chapters(self,duration):
        if self.chapter_status!='idle' or not self.mal_id or not self.episode:
            return
        self.chapter_status='loading'
        threading.Thread(target=self.fetch_chapters,args=(duration,),daemon=True).start()

    def fetch_chapters(self,duration):
        try:
            query=urllib.parse.urlencode([
                ('types','op'),('types','ed'),('types','recap'),('episodeLength',f'{duration:.3f}')
            ])
            url=f'https://api.aniskip.com/v2/skip-times/{urllib.parse.quote(self.mal_id)}/{urllib.parse.quote(self.episode)}?{query}'
            req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
            with urllib.request.urlopen(req,timeout=12) as resp:
                data=json.loads(resp.read().decode())
            labels={'op':'Intro','ed':'Credits','recap':'Recap'}
            chapters=[]
            for item in data.get('results',[]):
                kind=item.get('skipType')
                interval=item.get('interval') or {}
                if kind in labels:
                    chapters.append({
                        'type':kind,'label':labels[kind],
                        'start':float(interval.get('startTime',0)),
                        'end':float(interval.get('endTime',0)),
                    })
            with self.session_lock:
                self.chapters=chapters
                self.chapter_status='ready'
        except Exception:
            self.chapter_status='error'

    def subtitle_bytes(self,track_id,revision=None):
        snapshot=self.revision_payload(revision)
        if not snapshot:
            return None
        if track_id.startswith('original-'):
            track=next((x for x in snapshot.get('subtitle_tracks',[]) if x['id']==track_id),None)
            if not track:
                return None
            with self.req(track['url'],referrer=snapshot.get('referrer')) as resp:
                return resp.read()
        if track_id.startswith('generated-'):
            path=self.generated_path(
                track_id.removeprefix('generated-'),
                snapshot.get('episode'),
                snapshot.get('anime_id'),
            )
            return path.read_bytes() if path.exists() else None
        return None

    def select_subtitle(self,track_id):
        if track_id!='off' and not any(x['id']==track_id for x in self.subtitle_catalog()):
            raise RuntimeError('subtitle track not found')
        with self.session_lock:
            self.selected_subtitle_id=track_id
            self.pending_subtitle_id=None
        return self.session_json()

    def start_generation(self,track_id,select_when_ready=True):
        if not track_id.startswith('generated-'):
            raise RuntimeError('not an AI subtitle track')
        lang=track_id.removeprefix('generated-')
        if lang not in {code for code,_ in GENERATED_LANGUAGES}:
            raise RuntimeError('unsupported generated subtitle language')

        # Capture an immutable episode/revision snapshot. The user can keep
        # navigating while this worker runs without its media source changing.
        with self.session_lock:
            revision=int(self.revision)
            snapshot=self.revision_payload(revision)
        if not snapshot:
            raise RuntimeError('episode revision no longer available')
        episode=str(snapshot.get('episode') or '')
        anime_id=str(snapshot.get('anime_id') or self.anime_id)
        path=self.generated_path(lang,episode,anime_id)
        job_key=(anime_id,episode,lang)

        if path.exists():
            if select_when_ready:
                with self.session_lock:
                    if self.anime_id==anime_id and self.episode==episode:
                        self.selected_subtitle_id=track_id
            return self.session_json()

        with self.session_lock:
            running=self.ai_processes.get(job_key)
            if running is not None and running.poll() is None:
                if select_when_ready:
                    self.pending_subtitle_id=track_id
                return self.session_json()

        env_python=Path.home()/'.local/share/media-player/ai-env/bin/python'
        if not env_python.exists():
            raise RuntimeError('AI subtitle environment is not installed yet')

        out_dir=path.parent
        out_dir.mkdir(parents=True,exist_ok=True)
        status_path=path.with_name(f'generated-{lang}.status.json')
        status_path.write_text(json.dumps({'status':'starting','error':None}))
        logdir=Path(os.environ.get('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}'))/'ani-showtime'
        logdir.mkdir(parents=True,exist_ok=True)
        log_path=logdir/f'ai-{episode}-{lang}.log'
        log=log_path.open('ab')

        cmd=[
            str(env_python),str(self.root/'ai-subtitles.py'),
            '--media-url',f'{self.base_url}/master.m3u8?rev={revision}',
            '--output-dir',str(out_dir),'--target',lang,
        ]
        ai_env=os.environ.copy()
        driver_root=Path.home()/'.local/share/media-player/intel-gpu-runtime/root'
        driver_lib=driver_root/'usr/lib'
        driver_ocl=driver_lib/'intel-opencl'
        icd_dir=Path.home()/'.local/share/media-player/intel-gpu-runtime/icd'
        intel_icd=icd_dir/'intel.icd'
        if (driver_ocl/'libigdrcl.so').exists() and intel_icd.exists():
            old_ld=ai_env.get('LD_LIBRARY_PATH','')
            ai_env['LD_LIBRARY_PATH']=f'{driver_lib}:{driver_ocl}' + (f':{old_ld}' if old_ld else '')
            ai_env['OCL_ICD_VENDORS']=str(icd_dir)
        ai_env.setdefault('MEDIA_AI_ASR_QUALITY','quality')
        ai_env.setdefault('MEDIA_AI_OPENVINO_DEVICE','GPU')
        ai_env.setdefault('MEDIA_AI_TIMING_MODE','fast')

        proc=subprocess.Popen(
            cmd,env=ai_env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True
        )
        with self.session_lock:
            self.ai_processes[job_key]=proc
            if select_when_ready:
                self.pending_subtitle_id=track_id

        def monitor():
            code=proc.wait()
            log.close()
            with self.session_lock:
                self.ai_processes.pop(job_key,None)
                should_select=(
                    code==0
                    and path.exists()
                    and self.anime_id==anime_id
                    and self.episode==episode
                    and select_when_ready
                    and self.pending_subtitle_id==track_id
                )
                if should_select:
                    self.selected_subtitle_id=track_id
                    self.pending_subtitle_id=None
                elif self.pending_subtitle_id==track_id:
                    self.pending_subtitle_id=None
        threading.Thread(target=monitor,daemon=True).start()
        return self.session_json()

    def study_bytes(self,track_id,revision=None):
        snapshot=self.revision_payload(revision)
        if not snapshot or not track_id.startswith('study-ja-original-'):
            return None
        natural_id=track_id.removeprefix('study-ja-')
        path=self.study_path(natural_id,snapshot.get('episode'),snapshot.get('anime_id'))
        return path.read_bytes() if path.exists() else None

    def start_study(self,track_id):
        if not track_id.startswith('study-ja-original-'):
            raise RuntimeError('not a Japanese study subtitle track')
        natural_id=track_id.removeprefix('study-ja-')
        with self.session_lock:
            revision=int(self.revision)
            snapshot=self.revision_payload(revision)
        if not snapshot:
            raise RuntimeError('episode revision no longer available')
        natural_track=next((x for x in snapshot.get('subtitle_tracks',[]) if x.get('id')==natural_id),None)
        if not natural_track:
            raise RuntimeError('original translation track not found')
        if self._is_japanese_track(natural_track):
            raise RuntimeError('choose a non-Japanese original track for the natural translation')

        episode=str(snapshot.get('episode') or '')
        anime_id=str(snapshot.get('anime_id') or self.anime_id)
        path=self.study_path(natural_id,episode,anime_id)
        status_path=path.with_suffix('.status.json')
        job_key=(anime_id,episode,natural_id)
        if path.exists():
            with self.session_lock:
                if self.anime_id==anime_id and self.episode==episode:
                    self.selected_subtitle_id=track_id; self.pending_subtitle_id=None
            return self.session_json()
        with self.session_lock:
            running=self.study_processes.get(job_key)
            if running and running.is_alive():
                self.pending_subtitle_id=track_id
                return self.session_json()
            self.pending_subtitle_id=track_id

        path.parent.mkdir(parents=True,exist_ok=True)
        status_path.write_text(json.dumps({'status':'starting','error':None}))

        def worker():
            error=None
            try:
                env_python=Path.home()/'.local/share/media-player/ai-env/bin/python'
                if not env_python.exists(): raise RuntimeError('AI subtitle environment is not installed')
                japanese_track=self._japanese_original(snapshot)
                japanese_source='Original AniCLI Japanese'
                jp_file=path.parent/f'study-source-ja-{revision}.vtt'
                natural_file=path.parent/f'study-natural-{natural_id}-{revision}.vtt'

                status_path.write_text(json.dumps({'status':'preparing-japanese','error':None}))
                if japanese_track:
                    with self.req(japanese_track['url'],referrer=snapshot.get('referrer')) as resp:
                        jp_file.write_bytes(resp.read())
                else:
                    japanese_source='Qwen3-ASR 1.7B Japanese fallback'
                    ja_path=self.generated_path('ja',episode,anime_id)
                    if not ja_path.exists():
                        self.start_generation('generated-ja',select_when_ready=False)
                        deadline=time.monotonic()+1800
                        while time.monotonic()<deadline and not ja_path.exists():
                            state=self.generated_status('ja',episode,anime_id)
                            if state.get('status')=='error':
                                raise RuntimeError(state.get('error') or 'Japanese transcription failed')
                            time.sleep(.5)
                        if not ja_path.exists(): raise RuntimeError('Japanese transcription timed out')
                    jp_file.write_bytes(ja_path.read_bytes())

                status_path.write_text(json.dumps({'status':'analyzing','error':None}))
                with self.req(natural_track['url'],referrer=snapshot.get('referrer')) as resp:
                    natural_file.write_bytes(resp.read())
                cmd=[
                    str(env_python),str(self.root/'study-subtitles.py'),
                    '--japanese-vtt',str(jp_file),'--natural-vtt',str(natural_file),
                    '--output',str(path),'--natural-language',str(natural_track.get('language') or 'Original'),
                    '--japanese-source',japanese_source,
                ]
                subprocess.run(cmd,check=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=180)
                status_path.write_text(json.dumps({'status':'ready','error':None}))
            except Exception as exc:
                error=str(exc)
                status_path.write_text(json.dumps({'status':'error','error':error}))
            finally:
                for source in path.parent.glob(f'study-*-{revision}.vtt'):
                    source.unlink(missing_ok=True)
                with self.session_lock:
                    self.study_processes.pop(job_key,None)
                    if (
                        error is None and path.exists()
                        and self.anime_id==anime_id and self.episode==episode
                        and self.pending_subtitle_id==track_id
                    ):
                        self.selected_subtitle_id=track_id; self.pending_subtitle_id=None
                    elif self.pending_subtitle_id==track_id:
                        self.pending_subtitle_id=None

        thread=threading.Thread(target=worker,daemon=True,name=f'study-{episode}-{natural_id}')
        with self.session_lock: self.study_processes[job_key]=thread
        thread.start()
        return self.session_json()

    def resume_path(self,payload=None):
        if payload is None:
            anime_id=self.anime_id; episode=self.episode
        else:
            anime_id=payload.get('anime_id',self.anime_id); episode=payload.get('episode',self.episode)
        key=hashlib.sha256(f'{anime_id}:{episode}'.encode()).hexdigest()
        root=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'media-player'/'resume'
        root.mkdir(parents=True,exist_ok=True)
        return root/f'{key}.pos'

    def resume_us(self):
        try:
            return max(0,int(self.resume_path().read_text().strip()))
        except Exception:
            return 0

    def update_progress(self,position_ns,duration_ns,episode=None,revision=None):
        # Progress can race with an episode switch. Ignore stale samples.
        with self.session_lock:
            current_episode=str(self.episode)
            current_revision=int(self.revision)
        if episode is not None and str(episode)!=current_episode:
            return {'ok':True,'ignored':True,'reason':'episode-mismatch'}
        if revision is not None:
            try:
                if int(revision)!=current_revision:
                    return {'ok':True,'ignored':True,'reason':'revision-mismatch'}
            except (TypeError,ValueError):
                return {'ok':True,'ignored':True,'reason':'bad-revision'}

        path=self.resume_path()
        position_us=max(0,int(position_ns)//1000)
        duration_us=max(0,int(duration_ns)//1000)
        if duration_us and position_us>=duration_us-15_000_000:
            path.unlink(missing_ok=True)
        else:
            tmp=path.with_name(path.name+f'.tmp.{os.getpid()}')
            tmp.write_text(str(position_us)+chr(10)); os.replace(tmp,path)
        return {'ok':True}

    def req(self,url,range_header=None,referrer=None):
        headers={'Referer':referrer or self.referrer,'User-Agent':'Mozilla/5.0'}
        if range_header: headers['Range']=range_header
        last=None
        for attempt in range(4):
            try:
                return urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=20)
            except Exception as exc:
                last=exc
                if attempt < 3:
                    time.sleep(0.25 * (2 ** attempt))
        raise last

    @staticmethod
    def ts_offset(data):
        # The upstream CDN disguises MPEG-TS segments as .jpg files by
        # prepending a valid tiny PNG and some junk. mpv scans past it;
        # GStreamer's hlsdemux2 expects TS sync from the beginning. Find a
        # stable 188-byte sync alignment and expose only the actual TS bytes.
        limit=min(len(data),65536)
        for off in range(limit):
            if data[off] != 0x47:
                continue
            if all(off + n*188 < len(data) and data[off+n*188] == 0x47 for n in range(1,6)):
                return off
        return None

    def clean_segment(self,data,target):
        if not target.lower().endswith(('.jpg','.jpeg','.ts')):
            return data, None
        off=self.ts_offset(data)
        if off is None:
            return data, None
        return data[off:], off

    def cache_path(self,url):
        key=hashlib.sha256(url.encode()).hexdigest()
        return self.cache_root/key[:2]/key[2:]

    def cached_segment(self,url):
        path=self.cache_path(url)
        try:
            data=path.read_bytes()
            os.utime(path,None)
            return data
        except FileNotFoundError:
            return None

    def fetch_segment(self,url,referrer=None):
        data=self.cached_segment(url)
        if data is not None:
            return data
        with self.req(url,referrer=referrer) as resp:
            raw=resp.read()
        clean,_=self.clean_segment(raw,url)
        path=self.cache_path(url)
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_name(path.name+f'.tmp.{os.getpid()}.{threading.get_ident()}')
        try:
            tmp.write_bytes(clean); os.replace(tmp,path)
        finally:
            try: tmp.unlink()
            except FileNotFoundError: pass
        return clean

    def prefetch_url(self,url,referrer=None):
        with self.cache_lock:
            if url in self.prefetched: return
            self.prefetched.add(url)
        def work():
            try: self.fetch_segment(url,referrer)
            except Exception: pass
        self.prefetch.submit(work)

    def prune_cache(self):
        try:
            files=[p for p in self.cache_root.rglob('*') if p.is_file()]
            total=sum(p.stat().st_size for p in files)
            if total<=self.cache_max_bytes: return
            for path in sorted(files,key=lambda p:p.stat().st_mtime):
                try:
                    size=path.stat().st_size; path.unlink(); total-=size
                except FileNotFoundError: pass
                if total<=int(self.cache_max_bytes*.85): break
        except Exception: pass

    def rewrite_playlist(self,text,base,revision=None,referrer=None):
        if revision is None:
            revision=int(self.revision)
        else:
            revision=int(revision)
        snapshot=self.revision_payload(revision)
        if not snapshot:
            raise RuntimeError(f'media revision {revision} is no longer available')
        referrer=referrer or snapshot.get('referrer') or self.referrer

        out=[]; duration=0.0; durations=[]; segment_urls=[]
        uri_attr=re.compile(r'URI="([^"]+)"')
        previous_discontinuity=False

        def local_fetch_url(absolute):
            return f'/fetch?rev={revision}&u={urllib.parse.quote(absolute,safe="")}'

        for raw in text.splitlines():
            line=raw.strip()
            if line.startswith('#EXTINF:'):
                try:
                    segdur=float(line.split(':',1)[1].split(',',1)[0])
                    duration+=segdur
                    durations.append(segdur)
                except Exception:
                    pass
                out.append(raw); previous_discontinuity=False
                continue

            if line.startswith('#'):
                def repl(match):
                    absolute=urllib.parse.urljoin(base,match.group(1))
                    return f'URI="{local_fetch_url(absolute)}"'
                out.append(uri_attr.sub(repl,raw))
                previous_discontinuity=line.startswith('#EXT-X-DISCONTINUITY')
                continue

            if line:
                absolute=urllib.parse.urljoin(base,line)
                path=urllib.parse.urlparse(absolute).path.lower()
                is_segment=path.endswith(('.jpg','.jpeg','.ts'))
                if is_segment:
                    # GStreamer's tsdemux retains continuity state across HLS seeks.
                    # This CDN's segments are independently decodable but don't
                    # advertise that fact, so mark each TS as a fresh continuity
                    # period. Without this, arbitrary seeks produce CC mismatches
                    # and visible corruption even though the TS bytes are valid.
                    if not previous_discontinuity:
                        out.append('#EXT-X-DISCONTINUITY')
                    local=f'/segment.ts?rev={revision}&u={urllib.parse.quote(absolute,safe="")}'
                    segment_urls.append(absolute)
                else:
                    local=local_fetch_url(absolute)
                out.append(local)
                previous_discontinuity=False
            else:
                out.append(raw); previous_discontinuity=False

        is_current=revision==int(self.revision)
        if duration>0 and is_current:
            self.video_duration=duration
            if base==self.video:
                self.segment_durations=durations
                self.ensure_chapters(duration)

        # Current playback gets the full VOD cached; immutable/background
        # revisions warm a seek window without monopolising bandwidth.
        targets=segment_urls if is_current and base==self.video else segment_urls[:32]
        for url in targets:
            self.prefetch_url(url,referrer)
        return '\n'.join(out)+'\n'

    @staticmethod
    def _vtt_time(value):
        parts=value.strip().replace(',', '.').split(':')
        try:
            if len(parts)==2: return float(parts[0])*60+float(parts[1])
            if len(parts)==3: return float(parts[0])*3600+float(parts[1])*60+float(parts[2])
        except ValueError: pass
        return None

    def subtitle_cues(self):
        if self._subtitle_cues is not None: return self._subtitle_cues
        if not self.subtitle:
            self._subtitle_cues=[]
            return self._subtitle_cues
        with self.req(self.subtitle) as resp:
            text=resp.read().decode('utf-8','replace').replace('\r\n','\n').replace('\r','\n')
        cues=[]
        for block in re.split(r'\n{2,}',text.strip()):
            timing=next((line for line in block.splitlines() if '-->' in line),None)
            if not timing: continue
            left,right=timing.split('-->',1)
            start=self._vtt_time(left.split()[0])
            end=self._vtt_time(right.strip().split()[0])
            if start is not None and end is not None:
                cues.append((start,end,block))
        self._subtitle_cues=cues
        return cues

    def subtitle_segment(self,index):
        durations=self.segment_durations or ([self.video_duration] if self.video_duration else [3600.0])
        if index<0 or index>=len(durations): return None
        start=sum(durations[:index])
        end=start+durations[index]
        blocks=[block for cue_start,cue_end,block in self.subtitle_cues() if cue_start<end and cue_end>start]
        mpegts=max(0,round(start*90000))
        body=f'WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:{mpegts}\n\n'
        if blocks:
            body+='\n\n'.join(blocks)+'\n\n'
        return body.encode('utf-8')


def make_handler(proxy):
    class H(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*a): pass
        def send_bytes(self,data,ctype,status=200,extra=None):
            if status==200:
                data,status,range_extra=self.apply_range(data,self.headers.get('Range'))
                extra={**range_extra, **(extra or {})}
            try:
                self.send_response(status)
                self.send_header('Content-Type',ctype)
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Accept-Ranges','bytes')
                self.send_header('Cache-Control','no-store')
                if extra:
                    for k,v in extra.items(): self.send_header(k,v)
                self.end_headers()
                if self.command!='HEAD': self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                return

        @staticmethod
        def apply_range(data, header):
            if not header or not header.startswith('bytes='):
                return data, 200, {}
            spec=header[6:].split(',',1)[0].strip()
            try:
                left,right=spec.split('-',1)
                if left:
                    start=int(left); end=int(right) if right else len(data)-1
                else:
                    count=int(right); start=max(0,len(data)-count); end=len(data)-1
                if start < 0 or start >= len(data):
                    return b'', 416, {'Content-Range': f'bytes */{len(data)}'}
                end=min(end,len(data)-1)
                return data[start:end+1], 206, {
                    'Content-Range': f'bytes {start}-{end}/{len(data)}',
                    'Accept-Ranges': 'bytes',
                }
            except Exception:
                return data, 200, {}
        def send_json(self,obj,status=200):
            data=(json.dumps(obj,ensure_ascii=False)+chr(10)).encode()
            self.send_bytes(data,'application/json; charset=utf-8',status=status)

        def read_json(self):
            try:
                length=int(self.headers.get('Content-Length','0') or 0)
                raw=self.rfile.read(length) if length else b'{}'
                value=json.loads(raw.decode('utf-8','replace'))
                return value if isinstance(value,dict) else {}
            except Exception:
                return {}

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            u=urllib.parse.urlparse(self.path)
            q=urllib.parse.parse_qs(u.query)
            try:
                if u.path=='/api/handoff':
                    mode=(q.get('mode') or ['prefetch'])[0]
                    self.send_json(proxy.accept_handoff(self.read_json(),mode)); return
                if u.path=='/api/navigate':
                    direction=(q.get('direction') or ['next'])[0]
                    step=-1 if direction in ('previous','prev','-1') else 1
                    self.send_json(proxy.navigate(step)); return
                if u.path=='/api/subtitles/select':
                    body=self.read_json()
                    self.send_json(proxy.select_subtitle(str(body.get('id') or 'off'))); return
                if u.path=='/api/subtitles/generate':
                    body=self.read_json()
                    self.send_json(proxy.start_generation(str(body.get('id') or ''))); return
                if u.path=='/api/study/generate':
                    body=self.read_json()
                    self.send_json(proxy.start_study(str(body.get('id') or ''))); return
                if u.path=='/api/progress':
                    body=self.read_json()
                    self.send_json(proxy.update_progress(
                        int(body.get('position_ns') or 0),
                        int(body.get('duration_ns') or 0),
                        body.get('episode'),
                        body.get('revision'),
                    )); return
                self.send_error(404)
            except Exception as exc:
                self.send_json({'ok':False,'error':str(exc)},status=500)

        def do_GET(self):
            u=urllib.parse.urlparse(self.path)
            q=urllib.parse.parse_qs(u.query)
            def requested_revision():
                raw=(q.get('rev') or [str(proxy.revision)])[0]
                try:
                    return int(raw)
                except ValueError:
                    return None

            if u.path=='/api/session':
                self.send_json(proxy.session_json()); return
            if u.path=='/api/chapters':
                self.send_json({'chapters':list(proxy.chapters),'status':proxy.chapter_status}); return
            if u.path.startswith('/api/subtitles/') and u.path.endswith('.vtt'):
                track_id=urllib.parse.unquote(u.path[len('/api/subtitles/'):-4])
                revision=requested_revision()
                if revision is None:
                    self.send_error(400); return
                data=proxy.subtitle_bytes(track_id,revision)
                if data is None:
                    self.send_error(404); return
                self.send_bytes(data,'text/vtt; charset=utf-8'); return
            if u.path.startswith('/api/study/') and u.path.endswith('.json'):
                track_id=urllib.parse.unquote(u.path[len('/api/study/'):-5])
                revision=requested_revision()
                if revision is None:
                    self.send_error(400); return
                data=proxy.study_bytes(track_id,revision)
                if data is None:
                    self.send_error(404); return
                self.send_bytes(data,'application/json; charset=utf-8'); return

            if u.path in ('/master.m3u8','/video.m3u8'):
                revision=requested_revision()
                snapshot=proxy.revision_payload(revision)
                if revision is None or not snapshot:
                    self.send_error(410,'media revision expired'); return
                video=snapshot['video']
                referrer=snapshot.get('referrer') or proxy.referrer
                with proxy.req(video,referrer=referrer) as resp:
                    txt=resp.read().decode('utf-8','replace')
                if u.path=='/master.m3u8':
                    proxy.rewrite_playlist(txt,video,revision,referrer)
                    lines=['#EXTM3U','#EXT-X-VERSION:3']
                    lines.append('#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080')
                    lines.append(f'/video.m3u8?rev={revision}')
                    self.send_bytes(('\n'.join(lines)+'\n').encode(),'application/vnd.apple.mpegurl'); return
                data=proxy.rewrite_playlist(txt,video,revision,referrer).encode()
                self.send_bytes(data,'application/vnd.apple.mpegurl'); return

            # Legacy compatibility paths stay bound to the current revision.
            if u.path=='/subtitle.vtt' and proxy.subtitle:
                data=proxy.subtitle_bytes(proxy.selected_subtitle_id,proxy.revision)
                if data is None:
                    self.send_error(404); return
                self.send_bytes(data,'text/vtt; charset=utf-8'); return
            if u.path=='/subs.m3u8' and proxy.subtitle:
                durations=proxy.segment_durations or ([proxy.video_duration] if proxy.video_duration else [3600.0])
                target=max(1,math.ceil(max(durations)))
                lines=['#EXTM3U','#EXT-X-VERSION:3','#EXT-X-PLAYLIST-TYPE:VOD',f'#EXT-X-TARGETDURATION:{target}','#EXT-X-MEDIA-SEQUENCE:0']
                for i,dur in enumerate(durations):
                    lines.extend((f'#EXTINF:{dur:.6f},',f'/sub.vtt?i={i}'))
                lines.append('#EXT-X-ENDLIST')
                self.send_bytes(('\n'.join(lines)+'\n').encode(),'application/vnd.apple.mpegurl'); return
            if u.path=='/sub.vtt' and proxy.subtitle:
                try: index=int((q.get('i') or ['0'])[0])
                except ValueError: index=0
                data=proxy.subtitle_segment(index)
                if data is None:
                    self.send_error(404); return
                self.send_bytes(data,'text/vtt; charset=utf-8'); return

            if u.path in ('/fetch','/segment.ts'):
                target=(q.get('u') or [''])[0]
                revision=requested_revision()
                snapshot=proxy.revision_payload(revision)
                if revision is None or not snapshot:
                    self.send_error(410,'media revision expired'); return
                if not target.startswith(('http://','https://')):
                    self.send_error(400); return
                referrer=snapshot.get('referrer') or proxy.referrer
                try:
                    target_path=urllib.parse.urlparse(target).path.lower()
                    is_media_segment=target_path.endswith(('.jpg','.jpeg','.ts'))
                    if is_media_segment:
                        data=proxy.fetch_segment(target,referrer); ctype='video/mp2t'
                    else:
                        with proxy.req(target,referrer=referrer) as resp:
                            data=resp.read(); ctype=resp.headers.get('Content-Type','application/octet-stream')
                    if target.lower().endswith(('.m3u8','.m3u')) or 'mpegurl' in ctype.lower():
                        data=proxy.rewrite_playlist(
                            data.decode('utf-8','replace'),target,revision,referrer
                        ).encode()
                        ctype='application/vnd.apple.mpegurl'
                        self.send_bytes(data,ctype); return
                    if not is_media_segment:
                        clean,stripped=proxy.clean_segment(data,target)
                        if stripped is not None:
                            data=clean; ctype='video/mp2t'
                    self.send_bytes(data,ctype)
                except (BrokenPipeError,ConnectionResetError):
                    return
                except Exception as exc:
                    try: self.send_error(502,str(exc))
                    except (BrokenPipeError,ConnectionResetError): pass
                return
            self.send_error(404)
    return H

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--video',required=True); ap.add_argument('--subtitle',default=''); ap.add_argument('--referrer',required=True); ap.add_argument('--port',type=int,required=True)
    a=ap.parse_args(); p=Proxy(a.video,a.subtitle,a.referrer,a.port); s=QuietThreadingHTTPServer(('127.0.0.1',a.port),make_handler(p))
    print(f'http://127.0.0.1:{a.port}/master.m3u8',flush=True); s.serve_forever()
