#!/usr/bin/env python3
"""Build Japanese study cues from Japanese text + an untouched provider subtitle track."""
from __future__ import annotations
import argparse, html, json, re
from pathlib import Path

PARTICLE_GLOSSES={
    'は':'topic','が':'subject','を':'object','に':'to / at','で':'at / by','へ':'toward',
    'と':'with / quote','も':'also','の':'of / nominalizer','か':'question','から':'from / because',
    'まで':'until','より':'than / from','や':'and / etc.','ね':'right?','よ':'emphasis','ぞ':'emphasis',
    'って':'quote / emphasis','て':'and / -ing','な':'emphasis / huh','っけ':'was it? / recall',
    'だけ':'only','しか':'only','でも':'even / but','ので':'because','のに':'although','けど':'but',
    'けれど':'but','ばかり':'only / just','ほど':'extent / about',
}
AUX_GLOSSES={
    'だ':'be / copula','です':'be (polite)','た':'past','ない':'not','ぬ':'not','ん':'not / nominal',
    'ます':'polite','たい':'want to','れる':'passive / possible','られる':'passive / possible',
    'せる':'make / let','させる':'make / let','そう':'seems','よう':'seems / so that',
    'な':'copula / attributive','なら':'if / as for','だった':'was','でした':'was (polite)',
}
LEXICALIZED_NEGATIVES={'くだらない','つまらない','たまらない','しょうがない','仕方ない','もったいない'}
SUFFIX_GLOSSES={'たち':'plural','達':'plural','ら':'plural','ちゃん':'-chan','さん':'Mr./Ms.','さま':'-sama','様':'-sama','くん':'-kun','君':'-kun'}
COMMON_GLOSSES={
    'こと':'thing / fact','もの':'thing','何':'what','今':'now','ここ':'here','そこ':'there','あそこ':'over there',
    '僕':'I / me','俺':'I / me','私':'I / me','我々':'we / us','自分':'self / oneself',
    '君':'you','御前':'you','お前':'you','貴方':'you','あなた':'you',
    '彼':'he / him','彼女':'she / her','誰':'who','これ':'this','それ':'that / it','あれ':'that over there',
    '前':'before / in front of','後':'after / behind','良い':'good / well','まあ':'well / anyway',
    'そこそこ':'fairly / reasonably','ゆっくり':'slowly / leisurely','そう':'so / like that',
}
POS_GLOSSES={'名詞':'noun','動詞':'verb','形容詞':'adjective','副詞':'adverb','代名詞':'pronoun','接続詞':'conjunction','感動詞':'interjection','連体詞':'modifier','接頭辞':'prefix','接尾辞':'suffix'}
SEMANTIC_GROUPS=[
    {'monster','monsters','demon','demons','devil','devils','beast','beasts','creature','creatures'},
    {'hunt','hunts','hunting','kill','kills','killing','exterminate','extermination','eradicate','eradication','eliminate','elimination','destroy','destruction'},
    {'photo','photos','photograph','photographs','picture','pictures'},
    {'friend','friends','companion','companions'},
    {'talk','talks','talking','speak','speaks','speaking','converse','conversation','chat'},
    {'know','knows','knowing','known','understand','understands','understood','aware','unknown'},
    {'mage','mages','wizard','wizards','magician','magicians','magic'},
    {'certify','certified','certification','qualification','qualifications','class','grade'},
    {'go','goes','going','went','come','comes','coming','enter','enters','entering'},
]

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


def parse_vtt(path: Path,skip_bold_overlay: bool=False):
    text=path.read_text(encoding='utf-8-sig',errors='replace').replace('\r\n','\n').replace('\r','\n')
    cues=[]
    for block in re.split(r'\n{2,}',text):
        lines=[x.strip() for x in block.splitlines() if x.strip()]
        timing=next((i for i,x in enumerate(lines) if '-->' in x),None)
        if timing is None: continue
        left,right=lines[timing].split('-->',1)
        start=vtt_seconds(left); end=vtt_seconds(right.split()[0])
        if start is None or end is None or end<=start: continue
        raw_body='\n'.join(lines[timing+1:])
        if skip_bold_overlay and re.fullmatch(r'\s*<b>.*</b>\s*',raw_body,flags=re.S):
            continue
        body=html.unescape(re.sub(r'<[^>]+>','',raw_body)).strip()
        if body: cues.append((start,end,body))
    return cues


def katakana_to_hiragana(text: str) -> str:
    out=[]
    for ch in text:
        code=ord(ch)
        out.append(chr(code-0x60) if 0x30A1<=code<=0x30F6 else ch)
    return ''.join(out)


def is_kanji_char(ch: str) -> bool:
    return ('\u3400'<=ch<='\u4dbf' or '\u4e00'<=ch<='\u9fff' or '\uf900'<=ch<='\ufaff')


def ruby_segments(surface: str, reading_hiragana: str):
    """Align kana literals and attach hiragana only to actual kanji spans."""
    if not surface:
        return []
    grouped=[]
    for ch in surface:
        kind='kanji' if is_kanji_char(ch) else 'plain'
        if grouped and grouped[-1][0]==kind:
            grouped[-1]=(kind,grouped[-1][1]+ch)
        else:
            grouped.append((kind,ch))
    if not any(kind=='kanji' for kind,_ in grouped):
        return [{'text':surface}]

    reading=katakana_to_hiragana(reading_hiragana)
    result=[]; cursor=0
    for index,(kind,text) in enumerate(grouped):
        if kind=='plain':
            literal=katakana_to_hiragana(text)
            if reading.startswith(literal,cursor):
                cursor+=len(literal)
            else:
                found=reading.find(literal,cursor)
                if found>=0: cursor=found+len(literal)
            result.append({'text':text})
            continue

        next_literal=None
        for next_kind,next_text in grouped[index+1:]:
            if next_kind=='plain':
                next_literal=katakana_to_hiragana(next_text)
                break
        if next_literal:
            boundary=reading.find(next_literal,cursor)
            if boundary<0: boundary=len(reading)
        else:
            boundary=len(reading)
        furigana=reading[cursor:boundary]
        item={'text':text}
        if furigana: item['furigana']=furigana
        result.append(item)
        cursor=boundary
    return result


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


def build_analyzer(contextual_english: bool=True,asr_fallback: bool=False):
    from sudachipy import dictionary,tokenizer
    from jamdict import Jamdict
    sudachi=dictionary.Dictionary().tokenizer()
    split=tokenizer.Tokenizer.SplitMode.C
    jam=Jamdict()
    romanize=make_romanizer()
    gloss_cache={}

    def english_terms(text: str):
        return set(re.findall(r"[a-zA-Z']+",str(text or '').lower()))

    def semantic_group_hits(glosses,natural: str) -> int:
        natural_words=english_terms(natural)
        gloss_words=english_terms(' '.join(glosses))
        return sum(1 for group in SEMANTIC_GROUPS if natural_words & group and gloss_words & group)

    def semantic_score(glosses,natural: str) -> int:
        stop={'the','a','an','to','of','and','or','is','are','was','were','be','been','being','it','that','this','do','does','did','for','in','on'}
        natural_words=english_terms(natural)-stop
        gloss_words=english_terms(' '.join(glosses))-stop
        score=2*len(natural_words & gloss_words)
        score+=2*semantic_group_hits(glosses,natural)
        if gloss_words & {'i','me','you','he','she','we','us','they','them'} & natural_words:
            score+=3
        return score

    def entry_score(entry,natural: str):
        best=(-1,'')
        for sense in entry.senses[:8]:
            glosses=[g.text for g in sense.gloss]
            if not glosses: continue
            text=compact_gloss(glosses)
            candidate=(semantic_score(glosses,natural),text)
            if candidate[0]>best[0]: best=candidate
        return best

    def correct_asr_homophone(surface: str,reading: str,natural: str):
        # Only alter all-kanji ASR tokens, and only when a same-reading JMdict
        # alternative is much better supported by the untouched provider English.
        if not asr_fallback or not contextual_english or not natural:
            return surface,None
        if len(surface)<2 or surface in COMMON_GLOSSES or not all(is_kanji_char(ch) for ch in surface):
            return surface,None
        try:
            current_entries=jam.lookup(surface,strict_lookup=True,lookup_chars=False,lookup_ne=False).entries
            reading_entries=jam.lookup(reading,strict_lookup=True,lookup_chars=False,lookup_ne=False).entries
        except Exception:
            return surface,None
        current=max((entry_score(e,natural)[0] for e in current_entries[:4]),default=0)
        best=(current,surface,None)
        for entry in reading_entries[:16]:
            score,_=entry_score(entry,natural)
            group_hits=max((semantic_group_hits([g.text for g in sense.gloss],natural) for sense in entry.senses[:8]),default=0)
            if group_hits<2:
                continue
            for form in entry.kanji_forms[:1]:
                candidate=form.text
                if candidate==surface or len(candidate)!=len(surface): continue
                if not candidate or not all(is_kanji_char(ch) for ch in candidate): continue
                if score>best[0]: best=(score,candidate,surface)
        # Require a very strong contextual win. One matching English word is
        # not enough to rewrite kanji; this avoids false changes like 俺→爾.
        if best[1]!=surface and best[0]>=4 and best[0]>=current+4:
            return best[1],best[2]
        return surface,None

    def dictionary_gloss(query: str, fallback: str='', natural: str='') -> str:
        key=(query or fallback,natural.lower())
        if key in gloss_cache: return gloss_cache[key]
        best=None
        first=''
        for candidate in (query,fallback):
            if not candidate: continue
            try:
                entries=jam.lookup(candidate,strict_lookup=True,lookup_chars=False,lookup_ne=False).entries
            except Exception:
                entries=[]
            for entry in entries[:4]:
                for sense in entry.senses[:8]:
                    glosses=[g.text for g in sense.gloss]
                    if not glosses: continue
                    text=compact_gloss(glosses)
                    if not first: first=text
                    score=semantic_score(glosses,natural)
                    candidate_value=(score,-len(text),text)
                    if best is None or candidate_value>best:
                        best=candidate_value
            if best and best[0]>0: break
        result=best[2] if best and best[0]>0 else first
        gloss_cache[key]=result
        return result

    def has_exact(query: str) -> bool:
        try:
            return bool(jam.lookup(query,strict_lookup=True,lookup_chars=False,lookup_ne=False).entries)
        except Exception:
            return False

    def analyze(text: str,natural: str=''):
        if not contextual_english:
            natural=''
        morphs=list(sudachi.tokenize(text,split))
        tokens=[]
        i=0
        while i<len(morphs):
            m=morphs[i]; surface=m.surface(); pos=m.part_of_speech(); pos0=pos[0]
            if not surface.strip() or pos0=='空白':
                i+=1; continue
            if pos0=='補助記号' or surface in PUNCT:
                tokens.append({'surface':surface,'ruby':[{'text':surface}],'romaji':'','gloss':PUNCT.get(surface,surface),'pos':'punct'})
                i+=1; continue

            combined_surface=surface
            combined_reading=m.reading_form() if m.reading_form() not in ('','*') else surface
            normalized=m.normalized_form() if m.normalized_form() not in ('','*') else m.dictionary_form()
            lookup=normalized if normalized not in ('','*') else surface
            phrase_gloss=None
            if surface=='なん' and i+1<len(morphs) and morphs[i+1].surface()=='で':
                combined_surface='なんで'; combined_reading='ナンデ'; lookup='何で'
                phrase_gloss='why / how come'; i+=1

            # Prefer dictionary compounds (魔法 + 使い -> 魔法使い = wizard)
            # over misleading independent senses (magic + errand).
            if pos0=='名詞':
                consumed=0
                for extra in (2,1):
                    end=i+extra+1
                    if end>len(morphs): continue
                    chunk=morphs[i:end]
                    if not all(x.part_of_speech()[0] in ('名詞','接尾辞') for x in chunk): continue
                    candidate=''.join(x.surface() for x in chunk)
                    if has_exact(candidate):
                        combined_surface=candidate
                        combined_reading=''.join((x.reading_form() if x.reading_form() not in ('','*') else x.surface()) for x in chunk)
                        lookup=candidate; consumed=extra; break
                if consumed: i+=consumed

            # Only lexicalize known fixed negatives such as くだらない.
            # Ordinary verb negation stays tied to the dictionary verb so a
            # learner sees 知る + not rather than the unrelated adjective sense
            # of 知らない ("unknown / strange").
            if i+1<len(morphs):
                n=morphs[i+1]; npos=n.part_of_speech()
                if pos0 in ('動詞','形容詞') and npos[0]=='助動詞' and n.surface()=='ない':
                    candidate=combined_surface+n.surface()
                    if candidate in LEXICALIZED_NEGATIVES and has_exact(candidate):
                        combined_surface=candidate
                        nr=n.reading_form() if n.reading_form() not in ('','*') else n.surface()
                        combined_reading+=nr
                        lookup=candidate
                        i+=1

            # Keep conjugated lexical words readable for learners: 撮っ + て ->
            # 撮って / とって / totte, 知っ + た -> 知った / しった / shitta.
            # Auxiliaries remain represented as grammar hints on the lexical word.
            grammar=[]
            if pos0 in ('動詞','形容詞'):
                j=i+1
                while j<len(morphs):
                    n=morphs[j]; npos=n.part_of_speech(); ns=n.surface()
                    attach=False
                    if npos[0]=='助動詞':
                        attach=True
                        hint=AUX_GLOSSES.get(ns)
                        if hint and hint not in grammar: grammar.append(hint)
                    elif npos[0]=='助詞' and len(npos)>1 and npos[1]=='接続助詞' and ns in ('て','で'):
                        attach=True
                    if not attach: break
                    combined_surface+=ns
                    nr=n.reading_form() if n.reading_form() not in ('','*') else ns
                    combined_reading+=nr
                    i=j; j+=1
                    if npos[0]=='助詞': break

            hira=katakana_to_hiragana(combined_reading)
            original_asr_surface=None
            corrected,original_asr_surface=correct_asr_homophone(combined_surface,hira,natural)
            if corrected!=combined_surface:
                combined_surface=corrected; lookup=corrected
            if phrase_gloss:
                gloss=phrase_gloss
            elif pos0=='接尾辞' and combined_surface in SUFFIX_GLOSSES:
                gloss=SUFFIX_GLOSSES[combined_surface]
            elif pos0=='助詞':
                gloss=PARTICLE_GLOSSES.get(combined_surface,'particle')
            elif pos0=='助動詞':
                gloss=AUX_GLOSSES.get(combined_surface,'auxiliary')
            elif lookup in COMMON_GLOSSES:
                gloss=COMMON_GLOSSES[lookup]
            else:
                gloss=dictionary_gloss(lookup,combined_surface,natural)
                if not gloss:
                    if re.fullmatch(r'[゠-ヿー・]+',combined_surface):
                        gloss='name / loanword'
                    else:
                        gloss=POS_GLOSSES.get(pos0,'')
            if grammar:
                gloss=(gloss+' · '+', '.join(grammar)).strip(' ·')
                if len(gloss)>52: gloss=gloss[:49].rstrip()+'…'
            tokens.append({
                'surface':combined_surface,
                'ruby':ruby_segments(combined_surface,hira),
                'romaji':romanize(combined_reading,combined_surface,pos0),
                'gloss':gloss,
                'lemma':lookup,
                'pos':pos0,
                **({'asr_surface':original_asr_surface,'corrected':True} if original_asr_surface else {}),
            })
            i+=1
        return tokens
    return analyze


ALIGN_STOPWORDS={
    'the','a','an','to','of','and','or','is','are','was','were','be','been','being',
    'it','that','this','do','does','did','for','in','on','at','with','as','from','by',
    'particle','auxiliary','noun','verb','adjective','adverb','modifier','prefix','suffix',
}


def _alignment_terms(text: str):
    words=set(re.findall(r"[a-zA-Z']+",str(text or '').lower()))
    expanded=set(words)
    irregular={'known':'know','knows':'know','knowing':'know','went':'go','gone':'go','mages':'mage','wizards':'wizard','magicians':'magician'}
    for word in list(words):
        if word in irregular: expanded.add(irregular[word])
        if len(word)>4 and word.endswith('s'): expanded.add(word[:-1])
        if len(word)>5 and word.endswith('ing'): expanded.add(word[:-3])
        if len(word)>4 and word.endswith('ed'): expanded.add(word[:-2])
        if word.startswith('un') and len(word)>6: expanded.add(word[2:])
    return expanded-ALIGN_STOPWORDS


def _alignment_group_hits(left,right):
    return sum(1 for group in SEMANTIC_GROUPS if left & group and right & group)


def align_natural_sequence(japanese,natural,base_tokens,use_semantics=True):
    """Monotonic time + dictionary-semantic alignment to provider subtitles."""
    if not natural:
        return ['']*len(japanese)
    matches=[]
    last_idx=0
    for (start,end,_jp_text),tokens in zip(japanese,base_tokens):
        midpoint=(start+end)/2
        gloss_text=' '.join(str(t.get('gloss') or '') for t in tokens)
        jp_terms=_alignment_terms(gloss_text)
        jp_question=any(str(t.get('gloss') or '')=='question' for t in tokens)
        jp_negative=any('not' in str(t.get('gloss') or '').lower() for t in tokens)
        candidates=[]
        for idx in range(last_idx,len(natural)):
            nstart,nend,text=natural[idx]
            nmid=(nstart+nend)/2
            distance=abs(nmid-midpoint)
            overlap=max(0.0,min(end,nend)-max(start,nstart))
            if distance>6.0 and overlap<=0:
                if nstart>end+6.0:
                    break
                continue
            en_terms=_alignment_terms(text)
            lexical=len(jp_terms & en_terms) if use_semantics else 0
            group_hits=_alignment_group_hits(jp_terms,en_terms) if use_semantics else 0
            semantic=lexical*6 + group_hits*4
            if use_semantics and jp_question and '?' in text:
                semantic+=4
            if use_semantics and jp_negative and ("n't" in text.lower() or ' not ' in f' {text.lower()} '):
                semantic+=3
            ndur=max(.001,nend-nstart); jdur=max(.001,end-start)
            coverage=overlap/min(ndur,jdur) if overlap>0 else 0.0
            time_score=max(0.0,6.0-distance)*.75 + coverage*2.0 + min(overlap,3.0)*.35
            jump_penalty=max(0,idx-last_idx-5)*.2
            candidates.append((semantic+time_score-jump_penalty,semantic,time_score,-distance,-idx,idx,text))
        if not candidates:
            # Stay monotonic and fall back to the closest remaining provider cue.
            remaining=list(enumerate(natural[last_idx:],start=last_idx))
            if not remaining:
                matches.append(''); continue
            idx,item=min(remaining,key=lambda pair:abs(((pair[1][0]+pair[1][1])/2)-midpoint))
            matches.append(item[2]); last_idx=idx; continue
        chosen=max(candidates)
        idx=chosen[-2]; text=chosen[-1]
        matches.append(text)
        last_idx=idx
    return matches


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--japanese-vtt',type=Path,required=True)
    ap.add_argument('--natural-vtt',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--natural-language',default='English')
    ap.add_argument('--japanese-source',default='Japanese')
    args=ap.parse_args()
    japanese=parse_vtt(args.japanese_vtt); natural=parse_vtt(args.natural_vtt,skip_bold_overlay=True)
    if not japanese: raise SystemExit('Japanese subtitle source contains no cues')
    if not natural: raise SystemExit('Natural/original subtitle source contains no cues')
    contextual_english=args.natural_language.strip().lower().startswith('english')
    analyze=build_analyzer(
        contextual_english=contextual_english,
        asr_fallback=('asr' in args.japanese_source.lower() or 'qwen' in args.japanese_source.lower()),
    )
    base_tokens=[analyze(text,'') for _start,_end,text in japanese]
    aligned_natural=align_natural_sequence(japanese,natural,base_tokens,use_semantics=contextual_english)
    cues=[]
    for (start,end,text),natural_text in zip(japanese,aligned_natural):
        cues.append({
            'start_ns':int(start*1e9),'end_ns':int(end*1e9),
            'japanese':text,'natural':natural_text,
            'tokens':analyze(text,natural_text),
        })
    payload={
        'version':5,
        'japanese_source':args.japanese_source,
        'natural_source':f'Original AniCLI {args.natural_language}',
        'natural_language':args.natural_language,
        'gloss_language':'English (JMdict)',
        'cues':cues,
    }
    args.output.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.output.with_suffix(args.output.suffix+'.tmp')
    tmp.write_text(json.dumps(payload,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    tmp.replace(args.output)
    print(json.dumps({'ok':True,'cues':len(cues),'output':str(args.output)},ensure_ascii=False))

if __name__=='__main__':
    main()
