#!/usr/bin/env python3
"""Lazy local subtitle generation for the Showtime/ani-cli bridge."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import socket
import time
import urllib.request
from pathlib import Path

ASR_FAST_DEFAULT="Qwen/Qwen3-ASR-0.6B"
ASR_QUALITY_DEFAULT="Qwen/Qwen3-ASR-1.7B"
ASR_MODEL_OVERRIDE=os.environ.get("MEDIA_AI_ASR_MODEL","").strip() or None
OPENVINO_MODEL_OVERRIDE=os.environ.get("MEDIA_AI_OPENVINO_MODEL","").strip() or None
ALIGNER_DEFAULT=os.environ.get("MEDIA_AI_ALIGNER_MODEL","Qwen/Qwen3-ForcedAligner-0.6B")
TRANSLATOR_DEFAULT=os.environ.get("MEDIA_AI_TRANSLATION_MODEL","facebook/nllb-200-distilled-600M")
OPENVINO_FAST_MODEL_DEFAULT=str(Path.home()/".local/share/media-player/models/qwen3-asr-0.6b-openvino-int4")
OPENVINO_QUALITY_MODEL_DEFAULT=str(Path.home()/".local/share/media-player/models/qwen3-asr-1.7b-openvino-int8")
OPENVINO_DEVICE_DEFAULT=os.environ.get("MEDIA_AI_OPENVINO_DEVICE","GPU")
ASR_QUALITY_MODE_DEFAULT=os.environ.get("MEDIA_AI_ASR_QUALITY","quality").strip().lower()

LANGUAGE_NAMES = {
    'en': 'English',
    'de': 'German',
    'ja': 'Japanese',
    'af': 'Afrikaans',
    'ar': 'Arabic',
    'eu': 'Basque',
    'bn': 'Bengali',
    'bg': 'Bulgarian',
    'ca': 'Catalan',
    'zh': 'Chinese (Simplified)',
    'zh-TW': 'Chinese (Traditional)',
    'hr': 'Croatian',
    'cs': 'Czech',
    'da': 'Danish',
    'nl': 'Dutch',
    'et': 'Estonian',
    'fil': 'Filipino',
    'fi': 'Finnish',
    'fr': 'French',
    'gl': 'Galician',
    'el': 'Greek',
    'gu': 'Gujarati',
    'he': 'Hebrew',
    'hi': 'Hindi',
    'hu': 'Hungarian',
    'is': 'Icelandic',
    'id': 'Indonesian',
    'it': 'Italian',
    'kn': 'Kannada',
    'ko': 'Korean',
    'lv': 'Latvian',
    'lt': 'Lithuanian',
    'ms': 'Malay',
    'ml': 'Malayalam',
    'mt': 'Maltese',
    'mr': 'Marathi',
    'no': 'Norwegian',
    'fa': 'Persian',
    'pl': 'Polish',
    'pt': 'Portuguese',
    'pa': 'Punjabi',
    'ro': 'Romanian',
    'ru': 'Russian',
    'sr': 'Serbian',
    'sk': 'Slovak',
    'sl': 'Slovenian',
    'es': 'Spanish',
    'sw': 'Swahili',
    'sv': 'Swedish',
    'ta': 'Tamil',
    'te': 'Telugu',
    'th': 'Thai',
    'tr': 'Turkish',
    'uk': 'Ukrainian',
    'ur': 'Urdu',
    'vi': 'Vietnamese',
}

NLLB_TARGET_CODES = {
    'en': 'eng_Latn',
    'de': 'deu_Latn',
    'ja': 'jpn_Jpan',
    'af': 'afr_Latn',
    'ar': 'arb_Arab',
    'eu': 'eus_Latn',
    'bn': 'ben_Beng',
    'bg': 'bul_Cyrl',
    'ca': 'cat_Latn',
    'zh': 'zho_Hans',
    'zh-TW': 'zho_Hant',
    'hr': 'hrv_Latn',
    'cs': 'ces_Latn',
    'da': 'dan_Latn',
    'nl': 'nld_Latn',
    'et': 'est_Latn',
    'fil': 'tgl_Latn',
    'fi': 'fin_Latn',
    'fr': 'fra_Latn',
    'gl': 'glg_Latn',
    'el': 'ell_Grek',
    'gu': 'guj_Gujr',
    'he': 'heb_Hebr',
    'hi': 'hin_Deva',
    'hu': 'hun_Latn',
    'is': 'isl_Latn',
    'id': 'ind_Latn',
    'it': 'ita_Latn',
    'kn': 'kan_Knda',
    'ko': 'kor_Hang',
    'lv': 'lvs_Latn',
    'lt': 'lit_Latn',
    'ms': 'zsm_Latn',
    'ml': 'mal_Mlym',
    'mt': 'mlt_Latn',
    'mr': 'mar_Deva',
    'no': 'nob_Latn',
    'fa': 'pes_Arab',
    'pl': 'pol_Latn',
    'pt': 'por_Latn',
    'pa': 'pan_Guru',
    'ro': 'ron_Latn',
    'ru': 'rus_Cyrl',
    'sr': 'srp_Cyrl',
    'sk': 'slk_Latn',
    'sl': 'slv_Latn',
    'es': 'spa_Latn',
    'sw': 'swh_Latn',
    'sv': 'swe_Latn',
    'ta': 'tam_Taml',
    'te': 'tel_Telu',
    'th': 'tha_Thai',
    'tr': 'tur_Latn',
    'uk': 'ukr_Cyrl',
    'ur': 'urd_Arab',
    'vi': 'vie_Latn',
}


def resolve_asr_models(quality: str, explicit_asr: str | None = None, explicit_openvino: str | None = None) -> tuple[str,str]:
    quality=(quality or 'quality').strip().lower()
    if quality not in ('fast','quality'):
        quality='quality'
    asr_model=explicit_asr or ASR_MODEL_OVERRIDE or (ASR_QUALITY_DEFAULT if quality=='quality' else ASR_FAST_DEFAULT)
    if explicit_openvino or OPENVINO_MODEL_OVERRIDE:
        ov_model=explicit_openvino or OPENVINO_MODEL_OVERRIDE
    else:
        quality_path=Path(OPENVINO_QUALITY_MODEL_DEFAULT).expanduser()
        fast_path=Path(OPENVINO_FAST_MODEL_DEFAULT).expanduser()
        if quality=='quality' and quality_path.exists():
            ov_model=str(quality_path)
        else:
            ov_model=str(fast_path)
    return asr_model,ov_model


def vtt_time(seconds: float) -> str:
    ms=max(0,int(round(seconds*1000)))
    h,rem=divmod(ms,3_600_000); m,rem=divmod(rem,60_000); s,ms=divmod(rem,1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def write_vtt(path: Path,cues: list[tuple[float,float,str]]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+".tmp")
    with temp.open("w",encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for start,end,text in cues:
            text=" ".join(str(text).split()).strip()
            if not text: continue
            f.write(f"{vtt_time(start)} --> {vtt_time(max(end,start+.08))}\n{text}\n\n")
    os.replace(temp,path)


def parse_vtt(path: Path) -> list[tuple[float,float,str]]:
    def seconds(value: str) -> float:
        parts=value.replace(",",".").split(":")
        return float(parts[-1])+60*float(parts[-2])+(3600*float(parts[-3]) if len(parts)>2 else 0)
    text=path.read_text(encoding="utf-8",errors="replace").replace("\r","")
    cues=[]
    for block in re.split(r"\n{2,}",text):
        lines=[x.strip() for x in block.splitlines() if x.strip()]
        timing=next((i for i,x in enumerate(lines) if "-->" in x),None)
        if timing is None: continue
        left,right=lines[timing].split("-->",1)
        body=" ".join(lines[timing+1:]).strip()
        if body: cues.append((seconds(left.strip().split()[0]),seconds(right.strip().split()[0]),body))
    return cues


def extract_audio(media_url: str,wav: Path) -> None:
    if wav.exists() and wav.stat().st_size>1_000_000: return
    wav.parent.mkdir(parents=True,exist_ok=True)
    temp=wav.with_suffix(".tmp.wav")
    cmd=[
        "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","error",
        "-extension_picky","0","-allowed_extensions","ALL","-allowed_segment_extensions","ALL",
        "-i",media_url,
        "-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",str(temp),
    ]
    subprocess.run(cmd,check=True)
    os.replace(temp,wav)


def group_timestamps(items) -> list[tuple[float,float,str]]:
    cues=[]; words=[]; start=None; end=None
    for item in items or []:
        text=str(getattr(item,"text","") or "").strip()
        if not text: continue
        s=float(getattr(item,"start_time",0.0) or 0.0)
        e=float(getattr(item,"end_time",s+.1) or s+.1)
        if start is None: start=s
        end=e; words.append(text)
        joined="".join(words)
        terminal=bool(re.search(r"[。！？!?]$",text))
        if terminal or (end-start)>=4.5 or len(joined)>=48 or len(words)>=16:
            cues.append((start,end,joined)); words=[]; start=None; end=None
    if words and start is not None and end is not None:
        cues.append((start,end,"".join(words)))
    return cues


def generate_japanese_openvino(wav: Path,out_dir: Path,model_path: str,preferred_device: str,aligner_name: str) -> list[tuple[float,float,str]] | None:
    model_dir=Path(model_path).expanduser()
    if not model_dir.exists():
        return None
    try:
        import numpy as np
        import openvino as ov
        import openvino_genai as ov_genai
        import soundfile as sf
        import torch
        from qwen_asr import Qwen3ForcedAligner

        core=ov.Core()
        devices=list(core.available_devices)
        device=preferred_device if preferred_device in devices else ('GPU' if 'GPU' in devices else 'CPU')
        audio,sample_rate=sf.read(str(wav),dtype='float32',always_2d=False)
        audio=np.asarray(audio,dtype=np.float32)
        if audio.ndim>1:
            audio=audio.mean(axis=1)
        if sample_rate!=16000:
            raise RuntimeError(f'OpenVINO ASR expected 16 kHz audio, got {sample_rate}')

        openvino_cache=Path(os.environ.get('XDG_CACHE_HOME',str(Path.home()/'.cache'))) / 'media-player' / 'openvino'
        openvino_cache.mkdir(parents=True,exist_ok=True)
        pipeline=ov_genai.ASRPipeline(str(model_dir),device,CACHE_DIR=str(openvino_cache))
        timing_mode=os.environ.get('MEDIA_AI_TIMING_MODE','fast').strip().lower()

        # Fast path: Silero supplies real speech boundaries while Qwen runs only
        # once per long block. We then distribute Qwen's punctuated sentences
        # across the detected speech windows. This avoids the per-call overhead
        # of transcribing every 1-5 second VAD segment independently.
        if timing_mode != 'refine':
            try:
                from silero_vad import load_silero_vad, get_speech_timestamps
                vad_model=load_silero_vad()
                timestamps=get_speech_timestamps(
                    torch.from_numpy(audio),
                    vad_model,
                    sampling_rate=sample_rate,
                    return_seconds=True,
                    threshold=float(os.environ.get('MEDIA_AI_VAD_THRESHOLD','0.45')),
                    min_speech_duration_ms=180,
                    min_silence_duration_ms=300,
                    speech_pad_ms=180,
                    max_speech_duration_s=12,
                )

                merged=[]
                merge_gap=float(os.environ.get('MEDIA_AI_VAD_MERGE_GAP','2.0'))
                max_window=float(os.environ.get('MEDIA_AI_VAD_MAX_WINDOW','12'))
                for item in timestamps:
                    ws=float(item['start']); we=float(item['end'])
                    if merged and ws-merged[-1][1] <= merge_gap and we-merged[-1][0] <= max_window:
                        merged[-1]=(merged[-1][0],we)
                    else:
                        merged.append((ws,we))

                def distribute_text(text,windows):
                    pieces=[x.strip() for x in re.split(r'(?<=[。！？!?])',text) if x.strip()]
                    if not pieces:
                        pieces=[text.strip()] if text.strip() else []
                    expanded=[]
                    for piece in pieces:
                        if len(piece)>30:
                            parts=[x for x in re.split(r'(?<=[、,，；;])',piece) if x.strip()]
                            if len(parts)>1:
                                buffer=''
                                for part in parts:
                                    if buffer and len(buffer)+len(part)>30:
                                        expanded.append(buffer.strip()); buffer=part
                                    else:
                                        buffer+=part
                                if buffer.strip():
                                    expanded.append(buffer.strip())
                                continue
                        if len(piece)>52:
                            expanded.extend(piece[i:i+34] for i in range(0,len(piece),34))
                        else:
                            expanded.append(piece)
                    pieces=[x for x in expanded if x]
                    if not pieces or not windows:
                        return []

                    while len(pieces) < len(windows):
                        best_index=-1
                        best_parts=None
                        best_len=0
                        for i,piece in enumerate(pieces):
                            candidates=[x.strip() for x in re.split(r'(?<=[、,，；;])',piece) if x.strip()]
                            if len(candidates)>1 and len(piece)>best_len:
                                best_index=i; best_parts=candidates; best_len=len(piece)
                        if best_parts is None:
                            i=max(range(len(pieces)),key=lambda n:len(pieces[n]))
                            piece=pieces[i]
                            if len(piece)<6:
                                break
                            mid=len(piece)//2
                            best_index=i; best_parts=[piece[:mid].strip(),piece[mid:].strip()]
                        pieces=pieces[:best_index]+[x for x in best_parts if x]+pieces[best_index+1:]

                    durations=[max(.08,b-a) for a,b in windows]
                    total_dur=sum(durations)
                    chars=[max(1,len(x)) for x in pieces]
                    total_chars=sum(chars)

                    # Place each phrase into the speech window containing the
                    # same cumulative fraction of spoken time.
                    buckets=[[] for _ in windows]
                    char_cursor=0
                    dur_cumulative=[]
                    d=0.0
                    for duration in durations:
                        d+=duration
                        dur_cumulative.append(d/total_dur)
                    for piece,weight in zip(pieces,chars):
                        center=(char_cursor+weight/2)/total_chars
                        char_cursor+=weight
                        wi=next((i for i,x in enumerate(dur_cumulative) if center<=x),len(windows)-1)
                        buckets[wi].append(piece)

                    cues=[]
                    for (ws,we),bucket in zip(windows,buckets):
                        if not bucket:
                            continue
                        weights=[max(1,len(x)) for x in bucket]
                        weight_total=sum(weights)
                        cursor=ws
                        duration=max(.08,we-ws)
                        for piece,weight in zip(bucket,weights):
                            piece_end=min(we,cursor+duration*(weight/weight_total))
                            cues.append((cursor,piece_end,piece))
                            cursor=piece_end
                    return cues

                all_cues=[]
                transcripts=[]
                asr_block_seconds=float(os.environ.get('MEDIA_AI_ASR_BLOCK_SECONDS','45'))
                asr_max_new_tokens=int(os.environ.get('MEDIA_AI_ASR_MAX_NEW_TOKENS','512'))
                total_duration=len(audio)/sample_rate
                block_samples=max(sample_rate,int(asr_block_seconds*sample_rate))
                block_starts=list(range(0,len(audio),block_samples))
                total_blocks=max(1,len(block_starts))

                checkpoint_path=out_dir/'asr-fast-checkpoint.json'
                completed_blocks=0
                try:
                    checkpoint=json.loads(checkpoint_path.read_text())
                    if (
                        checkpoint.get('version')==1
                        and checkpoint.get('audio_samples')==len(audio)
                        and checkpoint.get('sample_rate')==sample_rate
                        and checkpoint.get('model')==str(model_dir)
                        and abs(float(checkpoint.get('block_seconds',0))-asr_block_seconds)<1e-6
                    ):
                        completed_blocks=max(0,min(int(checkpoint.get('completed_blocks',0)),total_blocks))
                        all_cues=[
                            (float(c[0]),float(c[1]),str(c[2]))
                            for c in checkpoint.get('cues',[])
                            if isinstance(c,list) and len(c)==3
                        ]
                        transcripts=[str(x) for x in checkpoint.get('transcripts',[])]
                except Exception:
                    completed_blocks=0
                    all_cues=[]
                    transcripts=[]

                def save_checkpoint(completed):
                    payload={
                        'version':1,
                        'audio_samples':len(audio),
                        'sample_rate':sample_rate,
                        'model':str(model_dir),
                        'block_seconds':asr_block_seconds,
                        'completed_blocks':completed,
                        'cues':[[a,b,t] for a,b,t in all_cues],
                        'transcripts':transcripts,
                    }
                    temp=checkpoint_path.with_suffix('.json.tmp')
                    temp.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
                    os.replace(temp,checkpoint_path)

                for index,start_sample in enumerate(block_starts,start=1):
                    if index<=completed_blocks:
                        continue
                    end_sample=min(len(audio),start_sample+int(asr_block_seconds*sample_rate))
                    block_start=start_sample/sample_rate
                    block_end=end_sample/sample_rate
                    windows=[
                        (max(a,block_start),min(b,block_end))
                        for a,b in merged if b>block_start and a<block_end and min(b,block_end)>max(a,block_start)
                    ]
                    if not windows:
                        continue
                    # Trim leading/trailing silence while retaining the real
                    # absolute positions for VTT cue placement.
                    crop_start=max(block_start,windows[0][0]-.25)
                    crop_end=min(block_end,windows[-1][1]+.25)
                    a=max(0,int(crop_start*sample_rate)); b=min(len(audio),int(crop_end*sample_rate))
                    chunk=audio[a:b]
                    if chunk.size==0:
                        continue
                    result=pipeline.generate(
                        chunk.tolist(),
                        language='Japanese',
                        return_timestamps=False,
                        max_new_tokens=asr_max_new_tokens,
                    )
                    text=''.join(str(x) for x in (result.texts or [])).strip()
                    if not text:
                        continue
                    transcripts.append(text)
                    all_cues.extend(distribute_text(text,windows))
                    save_checkpoint(index)
                    (out_dir/'asr-backend.json').write_text(json.dumps({
                        'backend':'openvino+silero-vad',
                        'device':device,
                        'model':str(model_dir),
                        'block':index,
                        'blocks':total_blocks,
                        'timing_mode':'fast',
                    },ensure_ascii=False),encoding='utf-8')

                if all_cues:
                    ja_path=out_dir/'generated-ja.vtt'
                    write_vtt(ja_path,all_cues)
                    (out_dir/'transcript-ja.txt').write_text('\n'.join(transcripts),encoding='utf-8')
                    return all_cues
            except Exception as exc:
                print(f'Fast VAD timing fallback: {exc}',file=sys.stderr,flush=True)

        aligner=None
        chunk_seconds=float(os.environ.get('MEDIA_AI_CHUNK_SECONDS','270'))
        chunk_samples=max(sample_rate,int(chunk_seconds*sample_rate))
        all_cues=[]
        transcripts=[]
        total_chunks=max(1,(len(audio)+chunk_samples-1)//chunk_samples)

        for index,start_sample in enumerate(range(0,len(audio),chunk_samples),start=1):
            chunk=audio[start_sample:start_sample+chunk_samples]
            if chunk.size==0:
                continue
            chunk_offset=start_sample/sample_rate
            result=pipeline.generate(
                chunk.tolist(),
                language='Japanese',
                return_timestamps=True,
                max_new_tokens=4096,
            )
            text=''.join(str(x) for x in (result.texts or [])).strip()
            if not text:
                continue
            transcripts.append(text)

            cues=[]
            chunks=list(result.chunks or [])
            if chunks:
                cues=[
                    (float(c.start_ts)+chunk_offset,float(c.end_ts)+chunk_offset,str(c.text).strip())
                    for c in chunks if str(c.text).strip()
                ]
            else:
                if aligner is None:
                    torch.set_num_threads(max(2,min(12,(os.cpu_count() or 8)//2)))
                    aligner=Qwen3ForcedAligner.from_pretrained(
                        aligner_name,
                        dtype=torch.bfloat16,
                        device_map='cpu',
                    )
                aligned=aligner.align(audio=(chunk,sample_rate),text=text,language='Japanese')
                if aligned:
                    grouped=group_timestamps(aligned[0].items)
                    cues=[(a+chunk_offset,b+chunk_offset,t) for a,b,t in grouped]

            if not cues:
                # Last-resort readable timing instead of failing the entire episode.
                pieces=[x for x in re.split(r'(?<=[。！？!?])',text) if x.strip()] or [text]
                duration=len(chunk)/sample_rate
                per=max(.5,duration/len(pieces))
                cues=[
                    (chunk_offset+i*per,chunk_offset+min(duration,(i+1)*per),piece.strip())
                    for i,piece in enumerate(pieces)
                ]
            all_cues.extend(cues)

            progress={
                'backend':'openvino+qwen-forced-aligner',
                'device':device,
                'model':str(model_dir),
                'aligner':aligner_name,
                'chunk':index,
                'chunks':total_chunks,
            }
            (out_dir/'asr-backend.json').write_text(json.dumps(progress,ensure_ascii=False),encoding='utf-8')

        if not all_cues:
            return None
        ja_path=out_dir/'generated-ja.vtt'
        write_vtt(ja_path,all_cues)
        (out_dir/'transcript-ja.txt').write_text('\n'.join(transcripts),encoding='utf-8')
        return all_cues
    except Exception as exc:
        print(f'OpenVINO ASR fallback: {exc}',file=sys.stderr,flush=True)
        return None

def generate_japanese(wav: Path,out_dir: Path,model_name: str,aligner_name: str,openvino_model: str,openvino_device: str=OPENVINO_DEVICE_DEFAULT) -> list[tuple[float,float,str]]:
    ja_path=out_dir/"generated-ja.vtt"
    backend_path=out_dir/"asr-backend.json"
    if ja_path.exists():
        # Never surprise the user by deleting a subtitle track that may already
        # be selected in Showtime. New episodes use the preferred model, while
        # existing episode caches are upgraded only when explicitly requested.
        upgrade_cache=os.environ.get("MEDIA_AI_ASR_UPGRADE_CACHE","0")=="1"
        if not upgrade_cache:
            return parse_vtt(ja_path)
        try:
            backend=json.loads(backend_path.read_text())
            cached_model=str(Path(str(backend.get("model") or "")).expanduser())
            wanted_model=str(Path(openvino_model).expanduser())
            if cached_model==wanted_model or not Path(wanted_model).exists():
                return parse_vtt(ja_path)
        except Exception:
            if not Path(openvino_model).exists():
                return parse_vtt(ja_path)
        ja_path.unlink(missing_ok=True)
        (out_dir/"generated-ja-romaji.vtt").unlink(missing_ok=True)
        (out_dir/"transcript-ja.txt").unlink(missing_ok=True)
        (out_dir/"asr-fast-checkpoint.json").unlink(missing_ok=True)

    cues=generate_japanese_openvino(wav,out_dir,openvino_model,openvino_device,aligner_name)
    if cues:
        return cues

    import torch
    from qwen_asr import Qwen3ASRModel
    torch.set_num_threads(max(2,min(12,(os.cpu_count() or 8)//2)))
    model=Qwen3ASRModel.from_pretrained(
        model_name,
        dtype=torch.float32,
        device_map="cpu",
        forced_aligner=aligner_name,
        forced_aligner_kwargs={"dtype":torch.float32,"device_map":"cpu"},
        max_inference_batch_size=1,
        max_new_tokens=1024,
    )
    results=model.transcribe(audio=str(wav),language="Japanese",return_time_stamps=True)
    if not results:
        raise RuntimeError("Qwen3-ASR returned no transcription")
    result=results[0]
    cues=group_timestamps(getattr(result,"time_stamps",None))
    if not cues:
        raise RuntimeError("Qwen3-ASR returned no timestamped cues")
    write_vtt(ja_path,cues)
    (out_dir/"transcript-ja.txt").write_text(str(getattr(result,"text","")),encoding="utf-8")
    return cues


def generate_romaji(cues,out_dir: Path) -> None:
    target=out_dir/"generated-ja-romaji.vtt"
    if target.exists(): return
    from pykakasi import kakasi
    converter=kakasi()
    try:
        from sudachipy import dictionary,tokenizer as sudachi_tokenizer
        sudachi=dictionary.Dictionary().tokenizer()
        split_mode=sudachi_tokenizer.Tokenizer.SplitMode.C
    except Exception:
        sudachi=None
        split_mode=None

    def romanize_reading(reading: str) -> str:
        return ''.join(x.get('hepburn') or x.get('orig') or '' for x in converter.convert(reading))

    def romanize(text: str) -> str:
        if sudachi is None:
            parts=converter.convert(text)
            value=' '.join(x.get('hepburn') or x.get('orig') or '' for x in parts)
            return re.sub(r'\s+([,.!?])',r'\1',re.sub(r'\s+',' ',value)).strip()

        groups=[]
        previous_pos=None
        previous_surface=''
        for morpheme in sudachi.tokenize(text,split_mode):
            surface=morpheme.surface()
            pos=morpheme.part_of_speech()
            if surface in ('、',',','，'):
                if groups: groups[-1]['text']=groups[-1]['text'].rstrip()+','
                else: groups.append({'text':',','pos':'記号'})
                previous_pos=pos; previous_surface=surface
                continue
            if surface in ('。','.','！','!','？','?'):
                mark={'。':'.','！':'!','？':'?'}.get(surface,surface)
                if groups: groups[-1]['text']=groups[-1]['text'].rstrip()+mark
                else: groups.append({'text':mark,'pos':'記号'})
                previous_pos=pos; previous_surface=surface
                continue
            if pos[0]=='助詞' and surface=='は':
                romaji='wa'
            elif pos[0]=='助詞' and surface=='へ':
                romaji='e'
            elif pos[0]=='助詞' and surface=='を':
                romaji='o'
            else:
                reading=morpheme.reading_form()
                if not reading or reading=='*':
                    reading=surface
                romaji=romanize_reading(reading)
            if not romaji:
                previous_pos=pos; previous_surface=surface
                continue

            join_previous=False
            # Inflectional endings belong to the preceding verb.
            if groups and pos[0]=='助動詞' and previous_pos and previous_pos[0]=='動詞':
                join_previous=True
            # Japanese te/de connective is romanized with the preceding verb.
            elif groups and pos[0]=='助詞' and len(pos)>1 and pos[1]=='接続助詞' and previous_pos and previous_pos[0]=='動詞':
                join_previous=True
            # だって is one spoken unit, not "da tte".
            elif groups and surface=='って' and previous_surface=='だ':
                join_previous=True

            if join_previous:
                groups[-1]['text']+=romaji
                # Keep the lexical verb identity through attached endings so a
                # following auxiliary (e.g. くだら + ない) also attaches.
                if previous_pos and previous_pos[0]=='動詞':
                    groups[-1]['pos']='動詞'
            else:
                groups.append({'text':romaji,'pos':pos[0]})
            previous_pos=pos; previous_surface=surface

        return re.sub(r'\s+([,.!?])',r'\1',' '.join(g['text'] for g in groups)).strip()

    converted=[]
    for start,end,text in cues:
        converted.append((start,end,romanize(text)))
    write_vtt(target,converted)

def translate_cues_translategemma(cues,out_dir: Path,target: str) -> bool:
    """Translate cues with a single temporary TranslateGemma llama.cpp server."""
    language_names=LANGUAGE_NAMES
    target_name=LANGUAGE_NAMES.get(target)
    if not target_name:
        return False

    llama_bin=Path(os.environ.get(
        'MEDIA_AI_TRANSLATEGEMMA_LLAMA',
        str(Path.home()/'.local/share/media-player/llama-static/build/bin/llama'),
    )).expanduser()
    gemma_model=Path(os.environ.get(
        'MEDIA_AI_TRANSLATEGEMMA_MODEL',
        str(Path.home()/'.local/share/media-player/models/translategemma-4b/translategemma-4b-it-Q4_K_M.gguf'),
    )).expanduser()
    if not llama_bin.exists() or not gemma_model.exists():
        return False

    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]

    device=os.environ.get('MEDIA_AI_TRANSLATEGEMMA_DEVICE','Vulkan0')
    context=max(2048,int(os.environ.get('MEDIA_AI_TRANSLATEGEMMA_CTX','4096')))
    cmd=[
        str(llama_bin),'serve','-m',str(gemma_model),
        '--device',device,'-ngl','all','-c',str(context),
        '--no-jinja','--chat-template','gemma',
        '--host','127.0.0.1','--port',str(port),'--log-disable',
    ]
    proc=subprocess.Popen(
        cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    base=f'http://127.0.0.1:{port}'
    try:
        deadline=time.monotonic()+float(os.environ.get('MEDIA_AI_TRANSLATEGEMMA_START_TIMEOUT','45'))
        while time.monotonic()<deadline:
            if proc.poll() is not None:
                raise RuntimeError(f'TranslateGemma server exited with code {proc.returncode}')
            try:
                with urllib.request.urlopen(base+'/health',timeout=1.5) as response:
                    if response.status==200:
                        break
            except Exception:
                time.sleep(.25)
        else:
            raise RuntimeError('TranslateGemma server startup timed out')

        batch_size=max(1,int(os.environ.get('MEDIA_AI_TRANSLATEGEMMA_BATCH','10')))
        translated=[]
        for offset in range(0,len(cues),batch_size):
            batch=cues[offset:offset+batch_size]
            numbered='\n'.join(f'{i+1}\t{text}' for i,(_,_,text) in enumerate(batch))
            prompt=(
                f'Translate each numbered Japanese anime subtitle line into {target_name}. '
                'Preserve names, numbers, meaning, and tone. Write concise natural subtitles. '
                'Return ONLY one line per input in the exact form N<TAB>translation, with the same numbering.\n'
                + numbered
            )
            payload=json.dumps({
                'model':'local','temperature':0,'max_tokens':max(128,len(batch)*48),
                'messages':[{'role':'user','content':prompt}],
            },ensure_ascii=False).encode('utf-8')
            req=urllib.request.Request(
                base+'/v1/chat/completions',data=payload,
                headers={'Content-Type':'application/json'},method='POST',
            )
            with urllib.request.urlopen(req,timeout=180) as response:
                result=json.loads(response.read().decode('utf-8','replace'))
            content=str(result['choices'][0]['message']['content']).strip()
            parsed={}
            for line in content.splitlines():
                match=re.match(r'^\s*(\d+)(?:\s*[	.)\]:-]\s*|\s+)(.+?)\s*$',line)
                if match:
                    value=match.group(2).strip()
                    if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"):
                        value=value[1:-1].strip()
                    parsed[int(match.group(1))]=value
            if len(parsed)!=len(batch) or any(i not in parsed for i in range(1,len(batch)+1)):
                raise RuntimeError(f'TranslateGemma returned {len(parsed)}/{len(batch)} aligned lines')
            for i,(start,end,_) in enumerate(batch,start=1):
                translated.append((start,end,parsed[i]))
            (out_dir/'translation-progress.json').write_text(json.dumps({
                'backend':'translategemma-4b-q4_k_m',
                'device':device,
                'target':target,
                'current':min(offset+len(batch),len(cues)),
                'total':len(cues),
            },ensure_ascii=False),encoding='utf-8')

        write_vtt(out_dir/f'generated-{target}.vtt',translated)
        (out_dir/'translation-backend.json').write_text(json.dumps({
            'backend':'translategemma-4b-q4_k_m','device':device,'model':str(gemma_model),
        },ensure_ascii=False),encoding='utf-8')
        return True
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


def translate_cues(cues,out_dir: Path,target: str,model_name: str) -> None:
    target_codes=NLLB_TARGET_CODES
    if target not in target_codes:
        raise RuntimeError(f'unsupported translation target: {target}')
    target_path=out_dir/f'generated-{target}.vtt'
    if target_path.exists():
        return

    backend=os.environ.get('MEDIA_AI_TRANSLATION_BACKEND','auto').strip().lower()
    if backend in ('auto','translategemma','gemma'):
        try:
            if translate_cues_translategemma(cues,out_dir,target):
                return
        except Exception as exc:
            print(f'TranslateGemma fallback: {exc}',file=sys.stderr,flush=True)
            if backend in ('translategemma','gemma'):
                raise

    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    torch.set_num_threads(max(2,min(int(os.environ.get('MEDIA_AI_TRANSLATION_THREADS','12')),os.cpu_count() or 8)))
    tokenizer=AutoTokenizer.from_pretrained(model_name,src_lang='jpn_Jpan')
    model=AutoModelForSeq2SeqLM.from_pretrained(model_name,torch_dtype=torch.float32)
    model.to('cpu'); model.eval()
    bos=tokenizer.convert_tokens_to_ids(target_codes[target])
    translated=[]
    batch_size=max(1,int(os.environ.get('MEDIA_AI_TRANSLATION_BATCH','8')))
    for offset in range(0,len(cues),batch_size):
        batch=cues[offset:offset+batch_size]
        texts=[x[2] for x in batch]
        encoded=tokenizer(texts,return_tensors='pt',padding=True,truncation=True,max_length=384)
        with torch.inference_mode():
            ids=model.generate(**encoded,forced_bos_token_id=bos,max_new_tokens=max(32,int(os.environ.get('MEDIA_AI_TRANSLATION_MAX_TOKENS','96'))),num_beams=max(1,int(os.environ.get('MEDIA_AI_TRANSLATION_BEAMS','1'))))
        outputs=tokenizer.batch_decode(ids,skip_special_tokens=True)
        for (start,end,_),text in zip(batch,outputs):
            translated.append((start,end,text.strip()))
        (out_dir/'translation-progress.json').write_text(json.dumps({
            'backend':'nllb-fallback',
            'device':'CPU',
            'target':target,
            'current':min(offset+len(batch),len(cues)),
            'total':len(cues),
        },ensure_ascii=False),encoding='utf-8')
    write_vtt(target_path,translated)
    (out_dir/'translation-backend.json').write_text(json.dumps({
        'backend':'nllb-fallback','device':'CPU','model':model_name,
    },ensure_ascii=False),encoding='utf-8')


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--media-url",required=True)
    ap.add_argument("--output-dir",required=True)
    ap.add_argument("--target",required=True,choices=tuple(LANGUAGE_NAMES)+("ja-romaji",))
    ap.add_argument("--asr-model",default=None)
    ap.add_argument("--aligner-model",default=ALIGNER_DEFAULT)
    ap.add_argument("--translation-model",default=TRANSLATOR_DEFAULT)
    ap.add_argument("--openvino-model",default=None)
    ap.add_argument("--asr-quality",choices=("fast","quality"),default=ASR_QUALITY_MODE_DEFAULT if ASR_QUALITY_MODE_DEFAULT in ("fast","quality") else "quality")
    ap.add_argument("--openvino-device",default=OPENVINO_DEVICE_DEFAULT)
    args=ap.parse_args()

    out_dir=Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True,exist_ok=True)
    status_path=out_dir/f"generated-{args.target}.status.json"

    def status(state,error=None):
        status_path.write_text(json.dumps({"status":state,"error":error},ensure_ascii=False),encoding="utf-8")

    try:
        # Different generated-language buttons can be clicked quickly. Serialize
        # heavy model work per episode so we never load two ASR models or race
        # the cached WAV/VTT files. The second request reuses everything produced
        # by the first one and usually only needs its translation stage.
        lock_path=out_dir/".generation.lock"
        with lock_path.open("w") as lock_file:
            fcntl.flock(lock_file.fileno(),fcntl.LOCK_EX)
            status("extracting")
            wav=out_dir/"audio-16k-mono.wav"
            extract_audio(args.media_url,wav)
            status("transcribing")
            asr_model,openvino_model=resolve_asr_models(args.asr_quality,args.asr_model,args.openvino_model)
            cues=generate_japanese(wav,out_dir,asr_model,args.aligner_model,openvino_model,args.openvino_device)
            generate_romaji(cues,out_dir)
            if args.target not in ("ja","ja-romaji"):
                status("translating")
                translate_cues(cues,out_dir,args.target,args.translation_model)
            status("ready")
        print(out_dir/f"generated-{args.target}.vtt")
        return 0
    except Exception as exc:
        status("error",str(exc))
        print(f"ai-subtitles: {exc}",file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
