#!/usr/bin/env python3
import importlib.util
import io
import json
import os
import tempfile
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ani_showtime_proxy',ROOT/'scripts/ani-showtime-proxy.py')
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

def make_proxy(tmp):
    os.environ['XDG_CACHE_HOME']=str(tmp/'cache')
    os.environ['XDG_STATE_HOME']=str(tmp/'state')
    os.environ['ANI_SHOWTIME_EPISODE']='12'
    os.environ['ANI_SHOWTIME_EPISODE_LIST']='12'
    os.environ['ANI_SHOWTIME_ANIME_ID']='test-anime-1'
    os.environ['ANI_SHOWTIME_SUBTITLE_CATALOG']='[]'
    proxy=mod.Proxy('https://example.invalid/video.m3u8','','https://example.invalid/',12345)
    proxy.prefetch_url=lambda *args,**kwargs: None
    return proxy

def test_discontinuities():
    with tempfile.TemporaryDirectory() as d:
        p=make_proxy(Path(d))
        source='#EXTM3U\n#EXTINF:5,\na.ts.jpg\n#EXTINF:5,\nb.ts.jpg\n#EXT-X-ENDLIST\n'
        out=p.rewrite_playlist(source,'https://cdn.invalid/x/')
        assert out.count('#EXT-X-DISCONTINUITY')==2,out
        assert out.count('/segment.ts?rev=1&u=')==2,out
        assert p.segment_durations==[], 'non-current playlist must not alter current segment durations'

def test_subtitle_catalog():
    raw=json.dumps([
        {'label':'English','src':'https://x/en.vtt','kind':'captions','default':True},
        {'label':'German','src':'https://x/de.vtt','kind':'captions','default':False},
    ])
    tracks=mod.Proxy.parse_subtitle_catalog(raw)
    assert [x['label'] for x in tracks]==['Original AniCLI English','Original AniCLI German']
    assert tracks[0]['default'] is True

def test_worker_root_exists():
    with tempfile.TemporaryDirectory() as d:
        p=make_proxy(Path(d))
        assert (p.root / "ai-subtitles.py").exists()

def test_revision_snapshot_stays_stable():
    with tempfile.TemporaryDirectory() as d:
        p=make_proxy(Path(d))
        first=p.revision_payload(1)
        assert first['video']=='https://example.invalid/video.m3u8'
        payload={
            'video':'https://example.invalid/episode13.m3u8',
            'subtitle':'',
            'referrer':'https://example.invalid/ref13/',
            'anime_id':'test-anime-1',
            'anime_title':'Test Anime',
            'episode':'13',
            'episode_list':['12','13','14'],
            'mal_id':'1',
            'mode':'sub',
            'quality':'best',
            'subtitle_tracks':[],
        }
        p.apply_payload(payload)
        assert p.revision==2
        assert p.revision_payload(1)['video']=='https://example.invalid/video.m3u8'
        assert p.revision_payload(1)['episode']=='12'
        assert p.revision_payload(2)['video']=='https://example.invalid/episode13.m3u8'
        assert p.revision_payload(2)['episode']=='13'

def test_revision_urls_are_pinned():
    with tempfile.TemporaryDirectory() as d:
        p=make_proxy(Path(d))
        source='#EXTM3U\n#EXTINF:5,\na.ts.jpg\n#EXT-X-ENDLIST\n'
        out=p.rewrite_playlist(source,'https://cdn.invalid/x/',revision=1,referrer='https://example.invalid/')
        assert '/segment.ts?rev=1&u=' in out,out

def test_generated_paths_are_episode_scoped():
    with tempfile.TemporaryDirectory() as d:
        p=make_proxy(Path(d))
        ep12=p.generated_path('ja','12','test-anime-1')
        ep13=p.generated_path('ja','13','test-anime-1')
        assert ep12!=ep13

def test_generated_status_reports_asr_progress():
    with tempfile.TemporaryDirectory() as d:
        p=make_proxy(Path(d))
        path=p.generated_path('en')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.with_name('generated-en.status.json').write_text(json.dumps({
            'status':'transcribing','error':None,
        }))
        (path.parent/'asr-backend.json').write_text(json.dumps({
            'backend':'openvino+silero-vad','device':'GPU','block':7,'blocks':33,
        }))
        state=p.generated_status('en')
        assert state['status']=='transcribing'
        assert state['current']==7
        assert state['total']==33
        assert state['backend']=='openvino+silero-vad'
        assert state['device']=='GPU'

def test_subtitle_selection_clears_pending_intent():
    with tempfile.TemporaryDirectory() as d:
        p=make_proxy(Path(d))
        p.pending_subtitle_id='generated-en'
        p.select_subtitle('off')
        assert p.pending_subtitle_id is None

def test_adjacent_prefetch_depths():
    with tempfile.TemporaryDirectory() as d:
        p=make_proxy(Path(d))
        playlist='#EXTM3U\n' + ''.join(f'#EXTINF:5,\nseg_{i:05d}.ts.jpg\n' for i in range(40)) + '#EXT-X-ENDLIST\n'
        p.req=lambda *_args,**_kwargs: io.BytesIO(playlist.encode())
        calls=[]
        p.prefetch_url=lambda url,*_args,**_kwargs: calls.append(url)
        payload={'video':'https://cdn.invalid/episode/index.m3u8','referrer':'https://ref.invalid/'}

        p.warm_payload(payload,None)
        assert len(calls)==40,len(calls)
        assert calls[0].endswith('seg_00000.ts.jpg') and calls[-1].endswith('seg_00039.ts.jpg')

        calls.clear()
        p.warm_payload(payload,24)
        assert len(calls)==24,len(calls)
        assert calls[-1].endswith('seg_00023.ts.jpg')

def test_study_language_continues_across_episode_switch():
    with tempfile.TemporaryDirectory() as d:
        p=make_proxy(Path(d))
        p.preferred_study_language='English'
        p.subtitle_tracks=[{'id':'original-0','source':'original','label':'Original AniCLI English','language':'English','url':'https://x/en12.vtt','default':True,'available':True}]
        started=[]
        p.start_study=lambda track_id: started.append(track_id) or p.session_json()
        payload={
            'video':'https://example.invalid/episode13.m3u8',
            'subtitle':'https://x/en13.vtt',
            'referrer':'https://example.invalid/ref13/',
            'anime_id':'test-anime-1','anime_title':'Test Anime','episode':'13',
            'episode_list':['12','13','14'],'mal_id':'1','mode':'sub','quality':'best',
            'subtitle_tracks':[{'id':'original-0','source':'original','label':'Original AniCLI English','language':'English','url':'https://x/en13.vtt','default':True,'available':True}],
        }
        p.apply_payload(payload)
        deadline=time.monotonic()+1
        while not started and time.monotonic()<deadline:
            time.sleep(.01)
        assert started==['study-ja-original-0'],started
        assert p.pending_subtitle_id=='study-ja-original-0'


def test_study_quality_lock_imported():
    assert hasattr(mod,"fcntl")
    assert hasattr(mod.fcntl,"flock")
    print("PASS test_study_quality_lock_imported")

def test_stale_progress_is_ignored():
    with tempfile.TemporaryDirectory() as d:
        p=make_proxy(Path(d))
        result=p.update_progress(30_000_000_000,1_400_000_000_000,episode='11',revision=1)
        assert result.get('ignored') is True,result
        assert not p.resume_path().exists()

if __name__=='__main__':
    tests=[v for k,v in globals().copy().items() if k.startswith('test_') and callable(v)]
    for test in tests:
        test(); print('PASS',test.__name__)
