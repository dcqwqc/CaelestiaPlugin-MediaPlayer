"""Persistent subtitle selection intent across media/episode generations.

Track IDs and ordering are per-file. A selected language/source is stable.
"""
import json
import os
import re
import tempfile
from pathlib import Path

ALIASES = {
    "eng": "en", "english": "en", "en": "en",
    "ara": "ar", "arabic": "ar", "ar": "ar",
    "deu": "de", "ger": "de", "german": "de", "de": "de",
    "jpn": "ja", "japanese": "ja", "ja": "ja",
    "spa": "es", "spanish": "es", "es": "es",
    "fra": "fr", "fre": "fr", "french": "fr", "fr": "fr",
    "hin": "hi", "hindi": "hi", "hi": "hi",
}

def language_tag(raw):
    value = str(raw or "").lower().strip()
    value = re.sub(r"^(original|ai generated|generated|captions|subtitle)\s+", "", value)
    if "romaji" in value and ("ja" in value or "japanese" in value):
        return "ja-romaji"
    words = re.findall(r"[a-z]+", value)
    if not words:
        return ""
    for word in words:
        if word in ALIASES:
            return ALIASES[word]
    return words[0] if len(words[0]) in (2, 3) else ""

def subtitle_intent(track):
    if not track:
        return None
    source = str(track.get("source") or "original").lower()
    kind = "generated" if source == "generated" else "study" if source == "study" else "original"
    language = language_tag(track.get("language")) or language_tag(track.get("label"))
    if not language:
        return None
    return kind, language

def matches_intent(intent, track):
    return intent is not None and intent == subtitle_intent(track)

def should_select_new_track(intent, track, requested_selected):
    """A manual choice always beats a provider's selected/default flag."""
    if intent == ("off", ""):
        return False
    if intent:
        return matches_intent(intent, track)
    return bool(requested_selected)


def preference_path():
    config_root=Path(os.environ.get("XDG_CONFIG_HOME") or Path.home()/".config")
    return config_root / "showtime-integration" / "subtitle-preference.json"

def load_preference(path=None):
    """Read only a language/source choice, never stream URLs or credentials."""
    path=Path(path) if path else preference_path()
    try:
        data=json.loads(path.read_text())
        candidate=(data.get("source"),data.get("language"))
        return candidate if _valid_preference(candidate) else ("original","en")
    except (OSError, ValueError, TypeError, AttributeError):
        return ("original","en")

def _valid_preference(pref):
    if not isinstance(pref,tuple) or len(pref)!=2:
        return False
    source,lang=pref
    return (source=="off" and lang=="") or (
        source in ("original","generated","study") and
        isinstance(lang,str) and bool(re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,10})?",lang))
    )

def save_preference(preference,path=None):
    if not _valid_preference(preference):
        return False
    path=Path(path) if path else preference_path()
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp=None
    try:
        with tempfile.NamedTemporaryFile(mode="w",dir=path.parent,prefix=".subtitle-",delete=False) as f:
            temp=Path(f.name)
            os.fchmod(f.fileno(),0o600)
            json.dump({"source":preference[0],"language":preference[1]},f)
            f.write(chr(10))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp,path)
        return True
    except OSError:
        if temp:
            try:temp.unlink(missing_ok=True)
            except OSError:pass
        return False
