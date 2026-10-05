#!/usr/bin/env python3
"""Build Japanese study cues from Japanese text + an untouched provider subtitle track."""
from __future__ import annotations
import argparse, html, json, re
from pathlib import Path

PARTICLE_GLOSSES={
    'は':'topic','が':'subject','を':'object','に':'to / at','で':'at / by','へ':'toward',
    'と':'with / quote','も':'also','の':'of / nominalizer','か':'question','から':'from / because',
    'まで':'until','より':'than / from','や':'and / etc.','ね':'right?','よ':'emphasis','ぞ':'emphasis',
    'って':'quote / emphasis','だけ':'only','しか':'only','でも':'even / but','ので':'because',
    'のに':'although','けど':'but','けれど':'but','ばかり':'only / just','ほど':'extent / about',
}
AUX_GLOSSES={
    'だ':'be / copula','です':'be (polite)','た':'past','ない':'not','ぬ':'not','ん':'not / nominal',
    'ます':'polite','たい':'want to','れる':'passive / possible','られる':'passive / possible',
    'せる':'make / let','させる':'make / let','そう':'seems','よう':'seems / so that',
    'な':'copula / attributive','なら':'if / as for','だった':'was','でした':'was (polite)',
}
PUNCT={'、':',','。':'.','！':'!','？':'?','…':'…','・':'·','「':'“','」':'”','『':'“','』':'”'}


def vtt_seconds(value: str) -> float | None:
    parts=value.strip().replace(',','.').split(':')
    try:
        if len(parts)==3: h,m,s=parts
        elif len(parts)==2: h='0';m,s=parts
        else: return None
        return int(h)*3600+int(m)*60+float(s)
    except Exception:
        return None


def parse_vtt(path: Path):
    text=path.read_text(encoding='utf-8-sig',errors='replace').replace('\r\n','\n').replace('\r','\n')
    cues=[]
    for block in re.split(r'\n{2,}',text):
        lines=[x.strip() for x in block.splitlines() if x.strip()]
        timing=next((i for i,x in enumerate(lines) if '-->' in x),None)
        if timing is None: continue
        left,right=lines[timing].split('-->',1)
        start=vtt_seconds(left); end=vtt_seconds(right.split()[0])
        if start is None or end is None or end<=start: continue
        body='\n'.join(lines[timing+1:])
        body=html.unescape(re.sub(r'<[^>]+>','',body)).strip()
        if body: cues.append((start,end,body))
    return cues


def katakana_to_hiragana(text: str) -> str:
    out=[]
    for ch in text:
        code=ord(ch)
        out.append(chr(code-0x60) if 0x30A1<=code<=0x30F6 else ch)
    return ''.join(out)


def make_romanizer():
    from pykakasi import kakasi
    conv=kakasi()
    def romanize(reading: str, surface: str='', pos0: str='') -> str:
        if pos0=='助詞':
            if surface=='は': return 'wa'
            if surface=='へ': return 'e'
            if surface=='を': return 'o'
        return ''.join(x.get('hepburn') or x.get('orig') or '' for x in conv.convert(reading)).strip()
    return romanize


def compact_gloss(values):
    seen=[]
    for value in values:
        value=re.sub(r'\s+',' ',str(value)).strip()
        if value and value not in seen: seen.append(value)
        if len(seen)>=2: break
    text=' / '.join(seen)
    return text if len(text)<=42 else text[:39].rstrip()+'…'


def build_analyzer():
    from sudachipy import dictionary,tokenizer
    from jamdict import Jamdict
    sudachi=dictionary.Dictionary().tokenizer()
    split=tokenizer.Tokenizer.SplitMode.C
    jam=Jamdict()
    romanize=make_romanizer()
    gloss_cache={}

    def dictionary_gloss(query: str, fallback: str='') -> str:
        key=query or fallback
        if key in gloss_cache: return gloss_cache[key]
        result=''
        for candidate in (query,fallback):
            if not candidate: continue
            try: entries=jam.lookup(candidate).entries
            except Exception: entries=[]
            if entries:
                glosses=[]
                for sense in entries[0].senses[:2]:
                    glosses.extend(g.text for g in sense.gloss)
                result=compact_gloss(glosses)
                if result: break
        gloss_cache[key]=result
        return result

    def analyze(text: str):
        morphs=list(sudachi.tokenize(text,split))
        tokens=[]
        i=0
        while i<len(morphs):
            m=morphs[i]; surface=m.surface(); pos=m.part_of_speech(); pos0=pos[0]
            if pos0=='補助記号' or surface in PUNCT:
                tokens.append({'surface':surface,'reading':'','katakana':'','romaji':'','gloss':PUNCT.get(surface,surface),'pos':'punct'})
                i+=1; continue

            # Sudachi analyzes くだらない as くだら + ない. Keep this lexicalized
            # adjective together so JMdict returns "trivial/silly", not 下る "go down".
            combined_surface=surface
            combined_reading=m.reading_form() if m.reading_form() not in ('','*') else surface
            lookup=m.dictionary_form() if m.dictionary_form() not in ('','*') else m.normalized_form()
            if i+1<len(morphs):
                n=morphs[i+1]; npos=n.part_of_speech()
                if pos0=='動詞' and npos[0]=='助動詞' and n.surface()=='ない':
                    candidate=surface+n.surface()
                    try: has_candidate=bool(jam.lookup(candidate).entries)
                    except Exception: has_candidate=False
                    if has_candidate:
                        combined_surface=candidate
                        nr=n.reading_form() if n.reading_form() not in ('','*') else n.surface()
                        combined_reading+=nr
                        lookup=candidate
                        i+=1

            hira=katakana_to_hiragana(combined_reading)
            if pos0=='助詞': gloss=PARTICLE_GLOSSES.get(combined_surface,'particle')
            elif pos0=='助動詞': gloss=AUX_GLOSSES.get(combined_surface,'auxiliary')
            else:
                gloss=dictionary_gloss(lookup,combined_surface)
                if not gloss:
                    gloss={'代名詞':'pronoun','接続詞':'conjunction','感動詞':'interjection'}.get(pos0,pos0)
            tokens.append({
                'surface':combined_surface,
                'reading':hira,
                'katakana':combined_reading,
                'romaji':romanize(combined_reading,combined_surface,pos0),
                'gloss':gloss,
                'lemma':lookup,
                'pos':pos0,
            })
            i+=1
        return tokens
    return analyze


def best_natural(start: float,end: float,natural):
    overlaps=[]
    for nstart,nend,text in natural:
        overlap=max(0.0,min(end,nend)-max(start,nstart))
        if overlap>0: overlaps.append((overlap,nstart,text))
    if overlaps:
        overlaps.sort(key=lambda x:x[1])
        ordered=[]
        for _,_,text in overlaps:
            if text not in ordered: ordered.append(text)
        return ' '.join(ordered)
    mid=(start+end)/2
    nearest=min(natural,key=lambda n:abs(((n[0]+n[1])/2)-mid),default=None)
    if nearest and abs(((nearest[0]+nearest[1])/2)-mid)<=3.0: return nearest[2]
    return ''


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--japanese-vtt',type=Path,required=True)
    ap.add_argument('--natural-vtt',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--natural-language',default='English')
    ap.add_argument('--japanese-source',default='Japanese')
    args=ap.parse_args()
    japanese=parse_vtt(args.japanese_vtt); natural=parse_vtt(args.natural_vtt)
    if not japanese: raise SystemExit('Japanese subtitle source contains no cues')
    if not natural: raise SystemExit('Natural/original subtitle source contains no cues')
    analyze=build_analyzer()
    cues=[]
    for start,end,text in japanese:
        cues.append({
            'start_ns':int(start*1e9),'end_ns':int(end*1e9),
            'japanese':text,'natural':best_natural(start,end,natural),
            'tokens':analyze(text),
        })
    payload={
        'version':1,
        'japanese_source':args.japanese_source,
        'natural_source':f'Original AniCLI {args.natural_language}',
        'natural_language':args.natural_language,
        'cues':cues,
    }
    args.output.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.output.with_suffix(args.output.suffix+'.tmp')
    tmp.write_text(json.dumps(payload,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    tmp.replace(args.output)
    print(json.dumps({'ok':True,'cues':len(cues),'output':str(args.output)},ensure_ascii=False))

if __name__=='__main__':
    main()
