"""Kokoro-82M on the CPU without any GPL code loaded (#104).

kokoro-onnx imports `phonemizer` and `espeakng_loader` (GPL) at module level and
loads espeak-ng.dll in Tokenizer.__init__; misaki.en imports `spacy` and `num2words`
(LGPL) at module level. `install_stubs()` registers stand-ins for all of them BEFORE
either package is imported; `_patch_misaki()` replaces G2P's tokenizer with a regex
tokenizer + a small heuristic Penn tagger that is phoneme-identical to spaCy on coach
speech (spike 2026-09-16); `_patch_kokoro()` makes Tokenizer a pure vocab lookup.
Pinned to the versions the spikes ran -- see the version constants.
"""
from __future__ import annotations

import io
import logging
import re
import struct
import sys
import threading
import types
from collections.abc import Callable
from pathlib import Path

import numpy as np

from simcoach import speech_numbers
from simcoach.pronunciations import apply as apply_pronunciations

KOKORO_ONNX_VERSION = "0.6.1"
MISAKI_VERSION = "0.7.4"
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
SAMPLE_RATE = 24_000
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_CAP_WORD = re.compile(r"\b[A-Z][a-z]+\b")
log = logging.getLogger(__name__)


class EngineUnavailable(RuntimeError):
    """The model is not installed or failed to load; callers fall back to Windows voices."""


# ---- GPL/LGPL import stubs -----------------------------------------------------------
def install_stubs() -> None:
    """Register stand-ins for phonemizer / espeakng_loader / spacy / num2words. Idempotent.
    Refuses if a REAL one is already imported: the adapter's guarantee is that they never are."""
    for name in ("phonemizer", "espeakng_loader", "spacy", "num2words"):
        mod = sys.modules.get(name)
        if mod is not None and not getattr(mod, "__simcoach_stub__", False):
            raise RuntimeError(f"{name} is already imported; the no-GPL adapter must load first")
    if getattr(sys.modules.get("phonemizer"), "__simcoach_stub__", False):
        return

    def _boom(*_a, **_k):
        raise RuntimeError("GPL backend stubbed out; phonemes come from misaki")

    ph = types.ModuleType("phonemizer"); ph.__simcoach_stub__ = True; ph.phonemize = _boom
    backend = types.ModuleType("phonemizer.backend")
    espeak = types.ModuleType("phonemizer.backend.espeak")
    wrapper = types.ModuleType("phonemizer.backend.espeak.wrapper")

    class _EspeakWrapper:
        @staticmethod
        def set_data_path(*_a, **_k): pass
        @staticmethod
        def set_library(*_a, **_k): pass

    wrapper.EspeakWrapper = _EspeakWrapper
    loader = types.ModuleType("espeakng_loader"); loader.__simcoach_stub__ = True
    loader.get_data_path = lambda: None; loader.get_library_path = lambda: None

    spacy = types.ModuleType("spacy"); spacy.__simcoach_stub__ = True
    spacy.load = _boom
    spacy.util = types.SimpleNamespace(is_package=lambda name: True)
    spacy.cli = types.SimpleNamespace(download=_boom)
    spacy.training = types.SimpleNamespace(Alignment=types.SimpleNamespace(from_strings=_boom))

    n2w = types.ModuleType("num2words"); n2w.__simcoach_stub__ = True
    n2w.num2words = speech_numbers.num2words

    for mod in (ph, backend, espeak, wrapper, loader, spacy, n2w):
        sys.modules[mod.__name__] = mod


# ---- misaki without spaCy (spike patch, verbatim) -----------------------------------
_TOKEN_RE = re.compile(
    r"\d+(?:[.:,]\d+)*%?"            # numbers, times, decimals, percents
    r"|[A-Za-z]+(?:['’][A-Za-z]+)*"  # words, incl. contractions
    r"|[^\sA-Za-z0-9]"               # single punctuation char
)
_CLOSED_TAGS = {"a": "DT", "an": "DT", "the": "DT", "to": "TO", "i": "PRP"}
_VERB_CUES = {"i", "you", "we", "they", "to", "will", "can", "did", "does", "do", "please", "let"}
_NOUN_CUES = {"a", "an", "the", "your", "my", "his", "her", "its", "their", "this", "that", "new", "a's"}
_ADJ_CUES = {"is", "was", "are", "were", "too", "very", "be"}
_PUNCT_TAGS = {".": ".", "!": ".", "?": ".", ",": ",", ":": ":", ";": ":", "-": ":", "–": ":",
               "(": "-LRB-", ")": "-RRB-", '"': "''", "'": "''", "$": "$", "%": "NFP", "#": "#"}


def _guess_tag(word: str, prev_lower: str | None, is_sent_start: bool) -> str:
    lw = word.lower()
    if lw in _CLOSED_TAGS:
        return _CLOSED_TAGS[lw]
    if word.isupper() and len(word) > 1:
        return "NNP"                         # acronym-shaped: ABS, TC
    if word[:1].isupper():
        return "VB" if is_sent_start else "NNP"   # coach cues are imperatives ("Record the lap")
    if prev_lower in _VERB_CUES:
        return "VBP"
    if prev_lower in _NOUN_CUES:
        return "NN"
    if prev_lower in _ADJ_CUES:
        return "JJ"
    return "NN"


def _simple_tokenize(text: str):
    from misaki.en import MToken
    matches = list(_TOKEN_RE.finditer(text))
    tokens, prev_lower, sent_start = [], None, True
    for idx, m in enumerate(matches):
        word = m.group(0)
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        whitespace = text[m.end():next_start]
        if word[0].isdigit():
            tag = "CD"
        elif word[0].isalpha():
            tag = _guess_tag(word, prev_lower, sent_start)
            prev_lower = word.lower()
        else:
            tag = _PUNCT_TAGS.get(word, ".")
        tokens.append(MToken(text=word, tag=tag, whitespace=whitespace))
        sent_start = word in (".", "!", "?")
    return tokens


def _tokenize_nospacy(self, text, tokens, features):
    """Drop-in for misaki.en.G2P.tokenize: maps inline-markup feature indices onto our
    fine tokens by consuming them until their concatenated text equals each coarse
    (whitespace-split) token -- what spaCy's Alignment computed before."""
    mutable = _simple_tokenize(text)
    if not features:
        return mutable
    j = 0
    for k, coarse in enumerate(tokens):
        js, acc = [], ""
        while j < len(mutable) and acc != coarse:
            if js:
                acc += mutable[js[-1]].whitespace
            acc += mutable[j].text
            js.append(j)
            j += 1
        if k not in features:
            continue
        v = features[k]
        for i, jj in enumerate(js):
            t = mutable[jj]
            if not isinstance(v, str):
                t.stress = v
            elif v.startswith("/"):
                t.is_head = i == 0
                t.phonemes = v.lstrip("/") if i == 0 else ""
                t.rating = 5
            elif v.startswith("#"):
                t.num_flags = v.lstrip("#")
    return mutable


_g2p = None
_g2p_lock = threading.Lock()


def _patch_misaki():
    from misaki import en

    def _init(self, trf=False, british=False, fallback=None, unk="❓"):
        self.british, self.nlp, self.lexicon, self.fallback, self.unk = british, None, en.Lexicon(british), None, unk

    en.G2P.__init__ = _init
    en.G2P.tokenize = _tokenize_nospacy
    return en.G2P()


def phonemize(text: str) -> str:
    """misaki English G2P, no spaCy, no espeak fallback. Raises on a token it can't resolve."""
    global _g2p
    install_stubs()
    with _g2p_lock:
        if _g2p is None:
            _g2p = _patch_misaki()
        phonemes, _tokens = _g2p(text)
    return phonemes


# ---- kokoro-onnx without espeak ------------------------------------------------------
def _patch_kokoro():
    install_stubs()
    import kokoro_onnx
    from kokoro_onnx.tokenizer import Tokenizer

    def _init(self, espeak_config=None, vocab=None):
        self.vocab = vocab or kokoro_onnx.config.DEFAULT_VOCAB   # no ctypes espeak load

    Tokenizer.__init__ = _init
    return kokoro_onnx.Kokoro


def preprocess(text: str, table: dict[str, str] | None) -> str:
    return apply_pronunciations(speech_numbers.expand(text), table or {})


def _wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    buf.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE")
    buf.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
    buf.write(b"data" + struct.pack("<I", len(pcm)) + pcm)
    return buf.getvalue()


class KokoroEngine:
    """One loaded Kokoro per process, lazy; synthesis serialised (CPU-bound, ordered)."""

    def __init__(self, model_dir: Path, backend: Callable | None = None, g2p: Callable[[str], str] | None = None):
        self.model_dir = Path(model_dir)
        self._backend = backend          # None = real kokoro-onnx via _patch_kokoro()
        self._g2p = g2p or phonemize
        self._kokoro = None
        self._lock = threading.Lock()
        self._error: str | None = None

    def available(self) -> bool:
        return (self.model_dir / MODEL_FILE).exists() and (self.model_dir / VOICES_FILE).exists()

    def _load(self):
        if self._kokoro is not None:
            return self._kokoro
        if not self.available():
            raise EngineUnavailable("Kokoro model is not installed")
        try:
            cls = self._backend or _patch_kokoro()
            self._kokoro = cls(str(self.model_dir / MODEL_FILE), str(self.model_dir / VOICES_FILE))
        except Exception as e:      # surfaced to Settings; Windows voices take over (re-raised, not swallowed)
            self._error = str(e) or type(e).__name__
            raise EngineUnavailable(self._error) from e
        return self._kokoro

    def voices(self) -> list[str]:
        return [v for v in self._load().get_voices() if v[:1] in ("a", "b")]   # American/British English

    def _phonemes_for(self, sentence: str) -> str | None:
        try:
            return self._g2p(sentence)
        except Exception as e:      # noqa: BLE001 -- misaki raises on an unknown proper noun
            word = str(e).strip("'\" ") or "?"
            if word in sentence:
                log.warning("kokoro g2p failed on %r (word %r); retrying spelled out", sentence, word)
                spelled = re.sub(re.escape(word), " ".join(word), sentence)
                try:
                    return self._g2p(spelled)
                except Exception:   # noqa: BLE001 -- spelled-out retry also failed; drop below
                    log.debug("kokoro g2p spelled-out retry also failed for %r", spelled)
            else:
                # Message doesn't name the failing word (e.g. misaki's bare TypeError on
                # an unknown token) -- try spelling out each capitalised word in turn.
                for m in _CAP_WORD.finditer(sentence):
                    cw = m.group(0)
                    spelled = sentence[:m.start()] + " ".join(cw) + sentence[m.end():]
                    log.warning("kokoro g2p failed on %r; retrying with %r spelled out", sentence, cw)
                    try:
                        return self._g2p(spelled)
                    except Exception:   # noqa: BLE001 -- try the next capitalised word
                        log.debug("kokoro g2p spelled-out retry also failed for %r", spelled)
                        continue
            log.warning("kokoro g2p dropped sentence %r", sentence)
            return None

    def synthesize(self, text: str, voice: str, speed: float, table: dict[str, str] | None = None) -> bytes:
        kokoro = self._load()
        prepared = preprocess(text, table)
        chunks = []
        with self._lock:
            for sentence in (s for s in _SENTENCE.split(prepared) if s.strip()):
                ph = self._phonemes_for(sentence)
                if not ph:
                    continue
                samples, rate = kokoro.create(ph, voice=voice, speed=float(speed), is_phonemes=True)
                chunks.append((np.asarray(samples, dtype=np.float32), rate))
        if not chunks:
            return _wav_bytes(np.zeros(0, dtype=np.float32), SAMPLE_RATE)
        rate = chunks[0][1]
        return _wav_bytes(np.concatenate([c for c, _ in chunks]), rate)
