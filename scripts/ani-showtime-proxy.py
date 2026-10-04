#!/usr/bin/env python3
import argparse, html, math, re, time, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

class Proxy:
    def __init__(self, video, subtitle, referrer):
        self.video=video; self.subtitle=subtitle; self.referrer=referrer
        self.video_duration=None
        self.segment_durations=[]
        self._subtitle_cues=None
    def req(self,url,range_header=None):
        headers={'Referer':self.referrer,'User-Agent':'Mozilla/5.0'}
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

    def rewrite_playlist(self,text,base):
        out=[]; duration=0.0; durations=[]
        uri_attr=re.compile(r'URI="([^"]+)"')
        for raw in text.splitlines():
            line=raw.strip()
            if line.startswith('#EXTINF:'):
                try:
                    segdur=float(line.split(':',1)[1].split(',',1)[0])
                    duration+=segdur
                    durations.append(segdur)
                except: pass
            if line.startswith('#'):
                def repl(m):
                    absu=urllib.parse.urljoin(base,m.group(1))
                    return 'URI="/fetch?u='+urllib.parse.quote(absu,safe='')+'"'
                out.append(uri_attr.sub(repl,raw)); continue
            if line:
                absu=urllib.parse.urljoin(base,line)
                out.append('/fetch?u='+urllib.parse.quote(absu,safe=''))
            else: out.append(raw)
        if duration>0:
            self.video_duration=duration
            if base == self.video:
                self.segment_durations=durations
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
        body='WEBVTT\n\n'
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
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u=urllib.parse.urlparse(self.path)
            if u.path=='/master.m3u8':
                # Prime duration from the actual media playlist.
                with proxy.req(proxy.video) as resp:
                    txt=resp.read().decode('utf-8','replace')
                proxy.rewrite_playlist(txt,proxy.video)
                lines=['#EXTM3U','#EXT-X-VERSION:3']
                if proxy.subtitle:
                    lines.append('#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,URI="/subs.m3u8"')
                    lines.append('#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,SUBTITLES="subs"')
                else:
                    lines.append('#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080')
                lines.append('/video.m3u8')
                self.send_bytes(('\n'.join(lines)+'\n').encode(),'application/vnd.apple.mpegurl'); return
            if u.path=='/video.m3u8':
                with proxy.req(proxy.video) as resp: txt=resp.read().decode('utf-8','replace')
                data=proxy.rewrite_playlist(txt,proxy.video).encode(); self.send_bytes(data,'application/vnd.apple.mpegurl'); return
            if u.path=='/subs.m3u8' and proxy.subtitle:
                durations=proxy.segment_durations or ([proxy.video_duration] if proxy.video_duration else [3600.0])
                target=max(1,math.ceil(max(durations)))
                lines=['#EXTM3U','#EXT-X-VERSION:3','#EXT-X-PLAYLIST-TYPE:VOD',f'#EXT-X-TARGETDURATION:{target}','#EXT-X-MEDIA-SEQUENCE:0']
                for i,dur in enumerate(durations):
                    lines.extend((f'#EXTINF:{dur:.6f},',f'/sub.vtt?i={i}'))
                lines.append('#EXT-X-ENDLIST')
                self.send_bytes(('\n'.join(lines)+'\n').encode(),'application/vnd.apple.mpegurl'); return
            if u.path=='/sub.vtt' and proxy.subtitle:
                try: index=int((urllib.parse.parse_qs(u.query).get('i') or ['0'])[0])
                except ValueError: index=0
                data=proxy.subtitle_segment(index)
                if data is None:
                    self.send_error(404); return
                self.send_bytes(data,'text/vtt; charset=utf-8'); return
            if u.path=='/fetch':
                q=urllib.parse.parse_qs(u.query); target=(q.get('u') or [''])[0]
                if not target.startswith(('http://','https://')):
                    self.send_error(400); return
                try:
                    # Fetch the complete upstream object first. For disguised
                    # .ts.jpg segments this lets us strip the PNG/junk prefix
                    # before applying the player's Range request to the clean
                    # MPEG-TS byte space.
                    with proxy.req(target) as resp:
                        data=resp.read(); ctype=resp.headers.get('Content-Type','application/octet-stream')
                    if target.lower().endswith(('.m3u8','.m3u')) or 'mpegurl' in ctype.lower():
                        data=proxy.rewrite_playlist(data.decode('utf-8','replace'),target).encode()
                        ctype='application/vnd.apple.mpegurl'
                        self.send_bytes(data,ctype); return

                    clean, stripped=proxy.clean_segment(data,target)
                    if stripped is not None:
                        data=clean; ctype='video/mp2t'

                    self.send_bytes(data,ctype)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as e:
                    try: self.send_error(502,str(e))
                    except (BrokenPipeError, ConnectionResetError): pass
                return
            self.send_error(404)
    return H

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--video',required=True); ap.add_argument('--subtitle',default=''); ap.add_argument('--referrer',required=True); ap.add_argument('--port',type=int,required=True)
    a=ap.parse_args(); p=Proxy(a.video,a.subtitle,a.referrer); s=ThreadingHTTPServer(('127.0.0.1',a.port),make_handler(p))
    print(f'http://127.0.0.1:{a.port}/master.m3u8',flush=True); s.serve_forever()
