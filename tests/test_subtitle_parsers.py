#!/usr/bin/env python3
import ast
import re
from pathlib import Path

SOURCE=Path('scripts/showtime-player.py')
NAMES={'_vtt_seconds','_parse_vtt','_parse_clock','_parse_srt','_parse_ass','_parse_subtitle_file'}
tree=ast.parse(SOURCE.read_text())
selected=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in NAMES]
ns={'re':re,'Path':Path}
exec(compile(ast.Module(body=selected,type_ignores=[]),str(SOURCE),'exec'),ns)


def check(name,data,filename,expected_text):
    cues=ns['_parse_subtitle_file'](data,filename)
    assert len(cues)==1,(name,cues)
    start,end,text=cues[0]
    assert start==1_000_000_000,(name,start)
    assert end==3_500_000_000,(name,end)
    assert expected_text in text,(name,text)
    print('PASS',name,text.replace('\n',' / '))

check('vtt',b'WEBVTT\n\n00:00:01.000 --> 00:00:03.500\nHello <i>VTT</i>\n','x.vtt','Hello VTT')
check('srt',b'1\n00:00:01,000 --> 00:00:03,500\nHello <b>SRT</b>\n','x.srt','Hello SRT')
ass = r'''[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Hello {\i1}ASS{\i0}\NLine 2
'''.encode()
check('ass',ass,'x.ass','Hello ASS\nLine 2')
