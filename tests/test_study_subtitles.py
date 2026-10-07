#!/usr/bin/env python3
import json, subprocess, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PYTHON=Path.home()/'.local/share/media-player/ai-env/bin/python'
SCRIPT=ROOT/'scripts/study-subtitles.py'

with tempfile.TemporaryDirectory() as d:
    d=Path(d)
    jp=d/'ja.vtt'; en=d/'en.vtt'; out=d/'study.json'
    jp.write_text("""WEBVTT

00:00:01.000 --> 00:00:04.000
お前は冒険者なのか。

00:00:05.000 --> 00:00:08.000
俺たちの夢だ。

00:00:09.000 --> 00:00:12.000
僕は行く。

00:00:13.000 --> 00:00:16.000
写真撮ってくれる。

00:00:17.000 --> 00:00:20.000
なんでだよ。
""")
    en.write_text("""WEBVTT

00:00:01.000 --> 00:00:04.000
So, you're an adventurer?

00:00:05.000 --> 00:00:08.000
It's our dream.

00:00:09.000 --> 00:00:12.000
I'll go.

00:00:13.000 --> 00:00:16.000
They'll take a photo for us.

00:00:17.000 --> 00:00:20.000
Why?
""")
    subprocess.run([
        str(PYTHON),str(SCRIPT),'--japanese-vtt',str(jp),'--natural-vtt',str(en),
        '--output',str(out),'--natural-language','English','--japanese-source','Original AniCLI Japanese',
    ],check=True,stdout=subprocess.DEVNULL)
    data=json.loads(out.read_text())
    assert data['version']==5
    assert data['natural_source']=='Original AniCLI English'
    assert data['japanese_source']=='Original AniCLI Japanese'
    assert data['gloss_language']=='English (JMdict)'
    assert len(data['cues'])==5

    first=data['cues'][0]
    assert first['natural']=="So, you're an adventurer?"
    by_surface={x['surface']:x for x in first['tokens']}
    assert by_surface['冒険者']['ruby']==[{'text':'冒険者','furigana':'ぼうけんしゃ'}]
    assert 'katakana' not in by_surface['冒険者'] and 'reading' not in by_surface['冒険者']
    assert by_surface['冒険者']['romaji']=='boukensha'
    assert 'adventurer' in by_surface['冒険者']['gloss']
    assert by_surface['お前']['gloss']=='you'
    assert by_surface['お前']['ruby']==[{'text':'お'},{'text':'前','furigana':'まえ'}]
    assert by_surface['は']['romaji']=='wa'
    assert by_surface['は']['gloss']=='topic'

    second={x['surface']:x for x in data['cues'][1]['tokens']}
    assert second['俺']['gloss']=='I / me'
    assert second['たち']['gloss']=='plural'

    third={x['surface']:x for x in data['cues'][2]['tokens']}
    assert third['僕']['gloss']=='I / me'

    fourth={x['surface']:x for x in data['cues'][3]['tokens']}
    assert fourth['撮って']['ruby']==[{'text':'撮','furigana':'と'},{'text':'って'}]
    assert fourth['撮って']['romaji']=='totte'

    fifth={x['surface']:x for x in data['cues'][4]['tokens']}
    assert fifth['なんで']['gloss']=='why / how come'
    assert all('katakana' not in token and 'reading' not in token for cue in data['cues'] for token in cue['tokens'])

with tempfile.TemporaryDirectory() as d:
    d=Path(d)
    jp=d/'ja.vtt'; en=d/'en.vtt'; out=d/'study-asr.json'
    jp.write_text("""WEBVTT

00:00:01.000 --> 00:00:04.000
厄介な魔物対峙だ。
""")
    en.write_text("""WEBVTT

00:00:01.000 --> 00:00:04.000
It's a troublesome monster hunt.
""")
    subprocess.run([
        str(PYTHON),str(SCRIPT),'--japanese-vtt',str(jp),'--natural-vtt',str(en),
        '--output',str(out),'--natural-language','English','--japanese-source','Qwen3-ASR 1.7B Japanese fallback',
    ],check=True,stdout=subprocess.DEVNULL)
    data=json.loads(out.read_text())
    corrected=next(x for x in data['cues'][0]['tokens'] if x.get('corrected'))
    assert corrected['asr_surface']=='対峙',corrected
    assert corrected['surface']=='退治',corrected
    assert 'extermination' in corrected['gloss'] or 'elimination' in corrected['gloss'],corrected

    provider=d/'provider.json'
    subprocess.run([
        str(PYTHON),str(SCRIPT),'--japanese-vtt',str(jp),'--natural-vtt',str(en),
        '--output',str(provider),'--natural-language','English','--japanese-source','Original AniCLI Japanese',
    ],check=True,stdout=subprocess.DEVNULL)
    original=json.loads(provider.read_text())['cues'][0]['tokens']
    assert any(x['surface']=='対峙' for x in original),original
    assert not any(x.get('corrected') for x in original),original

print('PASS study subtitle ruby/dictionary/alignment/context/conjugation/asr-homophone')

with tempfile.TemporaryDirectory() as d:
    d=Path(d)
    jp=d/'align-ja.vtt'; en=d/'align-en.vtt'; out=d/'align.json'
    jp.write_text("""WEBVTT

00:02:00.000 --> 00:02:03.200
フェルンが取ればいいじゃん。

00:02:03.367 --> 00:02:06.700
フリーレン様知らないんですか。

00:02:06.800 --> 00:02:13.231
一級魔法使いというのは熟練の魔法使いなんですよ。
""")
    en.write_text("""WEBVTT

00:01:59.770 --> 00:02:01.840
You should get certified, Fern.

00:02:01.840 --> 00:02:03.890
Ms. Frieren, don't you know?

00:02:04.530 --> 00:02:11.160
Only a handful of the most skilled mages have ever gained a first-class certification.
""")
    subprocess.run([
        str(PYTHON),str(SCRIPT),'--japanese-vtt',str(jp),'--natural-vtt',str(en),
        '--output',str(out),'--natural-language','English','--japanese-source','Qwen3-ASR 1.7B Japanese fallback',
    ],check=True,stdout=subprocess.DEVNULL)
    cues=json.loads(out.read_text())['cues']
    assert cues[0]['natural']=='You should get certified, Fern.',cues[0]
    assert cues[1]['natural']=="Ms. Frieren, don't you know?",cues[1]
    assert cues[2]['natural'].startswith('Only a handful'),cues[2]
    shiranai=next(t for t in cues[1]['tokens'] if t['surface']=='知らない')
    assert 'know' in shiranai['gloss'].lower() and 'not' in shiranai['gloss'].lower(),shiranai

print('PASS study subtitle monotonic semantic provider alignment')
