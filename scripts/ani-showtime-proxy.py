#!/usr/bin/env python3
import argparse, html, math, re, threading, time, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

class Proxy:
    def __init__(self, video, subtitle, referrer):
        self.video=video; self.subtitle=subtitle; self.referrer=referrer
        self.video_duration=None
        self.last_request=time.monotonic()
        self.seen_request=False
    def req(self,url,range_header=None):
        headers={'Referer':self.referrer,'User-Agent':'Mozilla/5.0'}
        if range_header: headers['Range']=range_header
        return urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=20)
    def rewrite_playlist(self,text,base):
        out=[]; duration=0.0
        uri_attr=re.compile(r'URI="([^"]+)"')
        for raw in text.splitlines():
            line=raw.strip()
            if line.startswith('#EXTINF:'):
                try: duration+=float(line.split(':',1)[1].split(',',1)[0])
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
        if duration>0: self.video_duration=duration
        return '\n'.join(out)+'\n'


def make_handler(proxy):
    class H(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*a): pass
        def send_bytes(self,data,ctype,status=200,extra=None):
            self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store')
            if extra:
                for k,v in extra.items(): self.send_header(k,v)
            self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            proxy.last_request=time.monotonic(); proxy.seen_request=True
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
                dur=proxy.video_duration or 3600.0; target=max(1,math.ceil(dur))
                txt=f'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-TARGETDURATION:{target}\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:{dur:.3f},\n/sub.vtt\n#EXT-X-ENDLIST\n'
                self.send_bytes(txt.encode(),'application/vnd.apple.mpegurl'); return
            if u.path=='/sub.vtt' and proxy.subtitle:
                with proxy.req(proxy.subtitle) as resp: data=resp.read()
                self.send_bytes(data,'text/vtt; charset=utf-8'); return
            if u.path=='/fetch':
                q=urllib.parse.parse_qs(u.query); target=(q.get('u') or [''])[0]
                if not target.startswith(('http://','https://')): self.send_error(400); return
                rng=self.headers.get('Range')
                try:
                    with proxy.req(target,rng) as resp:
                        data=resp.read(); ctype=resp.headers.get('Content-Type','application/octet-stream'); status=getattr(resp,'status',200)
                        if target.lower().endswith(('.m3u8','.m3u')) or 'mpegurl' in ctype.lower():
                            data=proxy.rewrite_playlist(data.decode('utf-8','replace'),target).encode(); ctype='application/vnd.apple.mpegurl'
                        elif target.lower().endswith('.jpg') and data[:1]==b'G':
                            ctype='video/mp2t'
                        extra={}
                        if resp.headers.get('Content-Range'): extra['Content-Range']=resp.headers['Content-Range']
                        if resp.headers.get('Accept-Ranges'): extra['Accept-Ranges']=resp.headers['Accept-Ranges']
                        self.send_bytes(data,ctype,status,extra)
                except Exception as e:
                    self.send_error(502,str(e))
                return
            self.send_error(404)
    return H

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--video',required=True); ap.add_argument('--subtitle',default=''); ap.add_argument('--referrer',required=True); ap.add_argument('--port',type=int,required=True)
    a=ap.parse_args(); p=Proxy(a.video,a.subtitle,a.referrer); s=ThreadingHTTPServer(('127.0.0.1',a.port),make_handler(p))
    def reap():
        while True:
            time.sleep(5)
            if p.seen_request and time.monotonic()-p.last_request>20:
                s.shutdown(); return
    threading.Thread(target=reap,daemon=True).start()
    print(f'http://127.0.0.1:{a.port}/master.m3u8',flush=True); s.serve_forever()
