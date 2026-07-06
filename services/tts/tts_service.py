# src/tts_service.py
"""Multi-provider TTS service — dispatches to Piper, Kokoro, dots.tts, API, or browser.

Provider tiers (see /api/tts/providers):
  kokoro        — fast natural default for live conversation (Apple Silicon MLX,
                  falls back to the torch pipeline on CUDA/MPS/CPU)
  piper         — lightweight always-works fallback (local CPU, .onnx voices)
  dots_tts_mlx  — experimental high-quality custom-voice / zero-shot cloning
                  engine on Apple Silicon (requires consented reference audio)
  endpoint:<id> — OpenAI-compatible /audio/speech via ModelEndpoint
  browser       — client-side Web Speech API (no server synthesis)
"""

import io
import time
import wave
import logging
import hashlib
import httpx
import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional, Dict, Any

from src.constants import DATA_DIR, TTS_CACHE_DIR, TTS_VOICES_FILE

logger = logging.getLogger(__name__)

PIPER_VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

PIPER_VOICE_CATALOG = [
    {"id": "en_US-lessac-medium", "language": "en", "locale": "en_US", "name": "Lessac", "quality": "medium", "gender": "female", "sample": "Hello, this is the English voice.", "path": "en/en_US/lessac/medium/en_US-lessac-medium"},
    {"id": "fr_FR-siwis-medium", "language": "fr", "locale": "fr_FR", "name": "Siwis", "quality": "medium", "gender": "female", "sample": "Bonjour, ceci est la voix française.", "path": "fr/fr_FR/siwis/medium/fr_FR-siwis-medium"},
    {"id": "es_ES-carlfm-x_low", "language": "es", "locale": "es_ES", "name": "Carl FM", "quality": "x_low", "gender": "male", "sample": "Hola, esta es la voz en español.", "path": "es/es_ES/carlfm/x_low/es_ES-carlfm-x_low"},
    {"id": "de_DE-thorsten-medium", "language": "de", "locale": "de_DE", "name": "Thorsten", "quality": "medium", "gender": "male", "sample": "Hallo, dies ist die deutsche Stimme.", "path": "de/de_DE/thorsten/medium/de_DE-thorsten-medium"},
    {"id": "it_IT-riccardo-x_low", "language": "it", "locale": "it_IT", "name": "Riccardo", "quality": "x_low", "gender": "male", "sample": "Ciao, questa è la voce italiana.", "path": "it/it_IT/riccardo/x_low/it_IT-riccardo-x_low"},
    {"id": "pt_BR-faber-medium", "language": "pt", "locale": "pt_BR", "name": "Faber", "quality": "medium", "gender": "male", "sample": "Olá, esta é a voz em português.", "path": "pt/pt_BR/faber/medium/pt_BR-faber-medium"},
    {"id": "nl_NL-mls-medium", "language": "nl", "locale": "nl_NL", "name": "MLS", "quality": "medium", "gender": "mixed", "sample": "Hallo, dit is de Nederlandse stem.", "path": "nl/nl_NL/mls/medium/nl_NL-mls-medium"},
    {"id": "pl_PL-darkman-medium", "language": "pl", "locale": "pl_PL", "name": "Darkman", "quality": "medium", "gender": "male", "sample": "Cześć, to jest polski głos.", "path": "pl/pl_PL/darkman/medium/pl_PL-darkman-medium"},
]

# Curated Kokoro voice ids (subset of the 54 shipped voices; af/am = American
# female/male, bf/bm = British female/male).
KOKORO_VOICE_CATALOG = [
    {"id": "af_heart", "name": "Heart", "gender": "female", "accent": "American"},
    {"id": "af_bella", "name": "Bella", "gender": "female", "accent": "American"},
    {"id": "af_nicole", "name": "Nicole", "gender": "female", "accent": "American"},
    {"id": "af_sarah", "name": "Sarah", "gender": "female", "accent": "American"},
    {"id": "am_adam", "name": "Adam", "gender": "male", "accent": "American"},
    {"id": "am_michael", "name": "Michael", "gender": "male", "accent": "American"},
    {"id": "bf_emma", "name": "Emma", "gender": "female", "accent": "British"},
    {"id": "bm_george", "name": "George", "gender": "male", "accent": "British"},
]


def _safe_speed(value, default: float = 1.0) -> float:
    """Parse the stored tts_speed defensively. The settings layer tolerates
    corrupt/agent-written config, so a non-numeric or empty value (e.g. an agent
    setting "speech speed" = "fast", or a hand-edited settings.json) must not
    crash synthesis or the stats endpoint with a ValueError."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default
    return speed if speed > 0 else default


def _wav_duration_seconds(data: bytes) -> float:
    """Duration of a WAV payload, or 0.0 if unparseable (e.g. mp3)."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            rate = wf.getframerate()
            return wf.getnframes() / rate if rate else 0.0
    except Exception:
        return 0.0


def _float_audio_to_wav(audio, sample_rate: int = 24000) -> Optional[bytes]:
    """Convert a float32 mono waveform (numpy-convertible) to 16-bit WAV bytes."""
    try:
        import numpy as np

        arr = np.asarray(audio, dtype="float32").reshape(-1)
        if arr.size == 0:
            return None
        arr = np.clip(arr, -1.0, 1.0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes((arr * 32767).astype(np.int16).tobytes())
        return buf.getvalue()
    except Exception as e:
        logger.error("Failed to encode waveform to WAV: %s", e, exc_info=True)
        return None


class TTSService:
    """Multi-provider TTS service.

    Reads provider config from data/settings.json on each call.
    Providers:
      "disabled"        — no TTS
      "browser"         — client-side Web Speech API (no server synthesis)
      "piper"           — Piper CLI using a local .onnx voice model
      "kokoro" / "local"— Kokoro-82M (MLX on Apple Silicon, else torch)
      "dots_tts_mlx"    — experimental dots.tts custom-voice engine (MLX)
      "endpoint:<id>"   — OpenAI-compatible /audio/speech via ModelEndpoint
    """

    def __init__(self, cache_dir: str = TTS_CACHE_DIR, voices_file: str = TTS_VOICES_FILE):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.voices_file = Path(voices_file)
        self._kokoro = None  # lazy-init
        self._dots = None    # lazy-init
        self._voices_lock = threading.Lock()
        # Rolling synthesis telemetry (updated from threadpool workers; simple
        # dict mutation under the GIL is fine for these counters).
        self.telemetry = {
            "synth_count": 0,
            "error_count": 0,
            "fallback_count": 0,
            "cache_hits": 0,
            "last_provider": "",
            "last_latency_ms": 0,
            "last_audio_seconds": 0.0,
            "last_rtf": 0.0,  # synthesis time / audio duration (lower is better)
        }

    # ── Settings ──

    def _load_settings(self) -> dict:
        from src.settings import load_settings
        saved = load_settings()
        return {
            "tts_enabled": saved.get("tts_enabled", True),
            "tts_provider": saved.get("tts_provider", "disabled"),
            "tts_model": saved.get("tts_model", "tts-1"),
            "tts_voice": saved.get("tts_voice", "alloy"),
            "tts_speed": saved.get("tts_speed", "1"),
            "tts_piper_voice_by_language": saved.get("tts_piper_voice_by_language", {}),
            "tts_piper_default_language": saved.get("tts_piper_default_language", "en"),
            "tts_piper_voices_dir": saved.get("tts_piper_voices_dir", ""),
            "tts_voice_profile": saved.get("tts_voice_profile", ""),
            "tts_kokoro_model": saved.get("tts_kokoro_model", "prince-canuma/Kokoro-82M"),
            "tts_dots_model": saved.get("tts_dots_model", ""),
        }

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        # "local" predates the provider tiers and always meant Kokoro.
        return "kokoro" if provider == "local" else provider

    @property
    def available(self) -> bool:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False:
            return False
        provider = self._effective_provider(settings)
        if provider == "disabled":
            return False
        if provider == "browser":
            return True  # handled client-side
        if provider == "piper":
            return self._piper_available(settings)
        if provider == "kokoro":
            # Kokoro loads lazily on first synthesis; report available when the
            # runtime is importable so the UI doesn't block on a model download.
            return self._get_kokoro().runtime_installed() or self._piper_available(settings)
        if provider == "dots_tts_mlx":
            return self._get_dots().configured(settings) or self._piper_available(settings)
        if provider.startswith("endpoint:"):
            return True  # assume reachable; errors surface at synthesis time
        return False

    def _effective_provider(self, settings: dict) -> str:
        """Provider after resolving the default voice profile, if any."""
        profile = self.get_voice_profile(settings.get("tts_voice_profile") or "")
        if profile:
            return self._normalize_provider(profile.get("provider") or "")
        return self._normalize_provider(settings["tts_provider"])

    # ── Cache ──

    def _cache_key(self, text: str, provider: str, model: str, voice: str, speed: float = 1.0) -> str:
        raw = f"{provider}|{model}|{voice}|{speed}|{text}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[bytes]:
        for ext in (".mp3", ".wav"):
            path = self.cache_dir / f"{key}{ext}"
            if path.exists():
                return path.read_bytes()
        return None

    def _put_cache(self, key: str, data: bytes):
        ext = ".mp3" if (len(data) >= 3 and (data[:3] == b'ID3' or (data[0] == 0xff and (data[1] & 0xe0) == 0xe0))) else ".wav"
        (self.cache_dir / f"{key}{ext}").write_bytes(data)

    def clear_cache(self):
        count = 0
        for f in self.cache_dir.glob("*.*"):
            f.unlink()
            count += 1
        logger.info(f"Cleared {count} cached TTS files")

    # ── Voice profiles ──
    #
    # A voice profile bundles everything needed to reproduce a voice: provider,
    # voice id / model path, speed, a sample sentence for previews, and — for
    # cloned/custom voices — consent and source notes. Stored in
    # data/tts-voices.json; the default profile id lives in settings
    # (tts_voice_profile) so it round-trips through the normal settings API.

    def _load_voices_file(self) -> dict:
        try:
            if self.voices_file.exists():
                data = json.loads(self.voices_file.read_text())
                if isinstance(data, dict) and isinstance(data.get("profiles"), list):
                    return data
        except Exception as e:
            logger.error("Failed to load %s: %s", self.voices_file, e)
        return {"profiles": []}

    def _save_voices_file(self, data: dict):
        self.voices_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.voices_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(self.voices_file)

    def list_voice_profiles(self) -> list[dict]:
        settings = self._load_settings()
        default_id = settings.get("tts_voice_profile") or ""
        profiles = self._load_voices_file()["profiles"]
        for p in profiles:
            p["is_default"] = p.get("id") == default_id
        return profiles

    def get_voice_profile(self, profile_id: str) -> Optional[dict]:
        if not profile_id:
            return None
        for p in self._load_voices_file()["profiles"]:
            if p.get("id") == profile_id:
                return p
        return None

    def save_voice_profile(self, profile: dict) -> dict:
        """Create or update a voice profile. Returns the stored profile."""
        provider = self._normalize_provider((profile.get("provider") or "").strip())
        if provider not in ("piper", "kokoro", "dots_tts_mlx") and not provider.startswith("endpoint:"):
            raise ValueError(f"unknown provider: {provider or '(empty)'}")
        name = (profile.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        stored = {
            "id": (profile.get("id") or "").strip() or uuid.uuid4().hex[:12],
            "name": name,
            "provider": provider,
            # voice id (kokoro/endpoint) or speaker id (piper)
            "voice": (profile.get("voice") or "").strip(),
            # .onnx path (piper) or HF repo / local model path (kokoro, dots)
            "model_path": (profile.get("model_path") or "").strip(),
            "speed": _safe_speed(profile.get("speed"), 1.0),
            "sample_text": (profile.get("sample_text") or "").strip() or "Hello! This is a preview of my voice.",
            # Custom-voice provenance — required for cloned voices.
            "consent_note": (profile.get("consent_note") or "").strip(),
            "source": (profile.get("source") or "").strip(),
            # Reference audio for zero-shot cloning (dots.tts)
            "ref_audio": (profile.get("ref_audio") or "").strip(),
            "ref_text": (profile.get("ref_text") or "").strip(),
            "created_at": profile.get("created_at") or time.time(),
        }
        if provider == "dots_tts_mlx" and stored["ref_audio"] and not stored["consent_note"]:
            raise ValueError("custom/cloned voices require a consent note describing permission to use the reference audio")
        with self._voices_lock:
            data = self._load_voices_file()
            existing = [p for p in data["profiles"] if p.get("id") != stored["id"]]
            existing.append(stored)
            data["profiles"] = existing
            self._save_voices_file(data)
        return stored

    def delete_voice_profile(self, profile_id: str) -> bool:
        with self._voices_lock:
            data = self._load_voices_file()
            before = len(data["profiles"])
            data["profiles"] = [p for p in data["profiles"] if p.get("id") != profile_id]
            if len(data["profiles"]) == before:
                return False
            self._save_voices_file(data)
        # Unset as default if it was the default
        from src.settings import load_settings, save_settings
        settings = load_settings()
        if settings.get("tts_voice_profile") == profile_id:
            settings["tts_voice_profile"] = ""
            save_settings(settings)
        return True

    def set_default_voice_profile(self, profile_id: str):
        from src.settings import load_settings, save_settings
        if profile_id and not self.get_voice_profile(profile_id):
            raise ValueError(f"voice profile not found: {profile_id}")
        settings = load_settings()
        settings["tts_voice_profile"] = profile_id or ""
        profile = self.get_voice_profile(profile_id) if profile_id else None
        if profile:
            # Keep the flat provider setting in sync so legacy consumers
            # (availability checks, stats) agree with the profile.
            settings["tts_provider"] = profile["provider"]
        save_settings(settings)

    # ── Providers overview ──

    def list_providers(self) -> list[dict]:
        settings = self._load_settings()
        kokoro = self._get_kokoro()
        dots = self._get_dots()
        piper_ok = self._piper_available(settings)
        active = self._effective_provider(settings)
        rows = [
            {
                "id": "kokoro",
                "label": "Kokoro-82M",
                "tier": "Fast natural (recommended for live chat)",
                "experimental": False,
                "installed": kokoro.runtime_installed(),
                "available": kokoro.runtime_installed(),
                "backend": kokoro.backend_name(),
                "model": settings.get("tts_kokoro_model"),
                "voices": KOKORO_VOICE_CATALOG,
                "hint": "" if kokoro.runtime_installed() else "Install with: pip install mlx-audio (Apple Silicon) or pip install kokoro soundfile",
            },
            {
                "id": "piper",
                "label": "Piper",
                "tier": "Lightweight fallback (always works)",
                "experimental": False,
                "installed": bool(shutil.which(self._piper_bin())),
                "available": piper_ok,
                "backend": "cli",
                "model": self._piper_model(settings),
                "voices": [],
                "hint": "" if piper_ok else "Install piper and download a voice from the Voice Library",
            },
            {
                "id": "dots_tts_mlx",
                "label": "dots.tts (MLX)",
                "tier": "Experimental — custom voices / cloning (consented audio only)",
                "experimental": True,
                "installed": dots.runtime_installed(),
                "available": dots.configured(settings),
                "backend": "mlx",
                "model": settings.get("tts_dots_model") or "(not configured)",
                "voices": [],
                "hint": dots.hint(settings),
            },
        ]
        for row in rows:
            row["active"] = row["id"] == active
        return rows

    # ── Kokoro ──

    def _get_kokoro(self):
        if self._kokoro is None:
            self._kokoro = _KokoroEngine()
        return self._kokoro

    # ── dots.tts (MLX) ──

    def _get_dots(self):
        if self._dots is None:
            self._dots = _DotsTtsMlx()
        return self._dots

    # ── Piper (local CPU TTS) ──

    def _piper_bin(self) -> str:
        return os.getenv("ODYSSEUS_PIPER_BIN", "piper")

    def _piper_voices_dir(self, settings: Optional[dict] = None) -> Path:
        settings = settings or self._load_settings()
        configured = settings.get("tts_piper_voices_dir") or ""
        return Path(configured or os.path.join(DATA_DIR, "piper-voices")).expanduser()

    def _piper_model(self, settings: dict, language: str = "", model_override: str = "") -> str:
        if model_override:
            return model_override
        lang = (language or "").lower().split("-", 1)[0]
        voice_map = settings.get("tts_piper_voice_by_language") or {}
        if lang and isinstance(voice_map, dict) and voice_map.get(lang):
            return voice_map[lang]
        default_lang = (settings.get("tts_piper_default_language") or "en").lower().split("-", 1)[0]
        if isinstance(voice_map, dict) and voice_map.get(default_lang):
            return voice_map[default_lang]
        configured = settings.get("tts_model") or ""
        if configured and configured not in {"tts-1", "tts-1-hd", "gpt-4o-mini-tts"}:
            return configured
        return os.getenv("ODYSSEUS_PIPER_MODEL", "")

    def _piper_available(self, settings: dict) -> bool:
        binary = shutil.which(self._piper_bin())
        model = self._piper_model(settings)
        return bool(binary and model and Path(model).expanduser().exists())

    def _synthesize_piper(self, text: str, settings: dict, speed: float = 1.0, language: str = "", model_override: str = "", voice_override: str = "") -> Optional[bytes]:
        binary = shutil.which(self._piper_bin())
        model = self._piper_model(settings, language=language, model_override=model_override)
        if not binary:
            logger.warning("Piper TTS not available. Install with: pip install piper-tts")
            return None
        if not model:
            logger.warning("Piper TTS model missing. Set ODYSSEUS_PIPER_MODEL or tts_model to a Piper .onnx file.")
            return None

        model_path = Path(model).expanduser()
        if not model_path.exists():
            logger.warning("Piper TTS model path does not exist: %s", model_path)
            return None

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name

            cmd = [binary, "--model", str(model_path), "--output_file", tmp_path]
            config_path = os.getenv("ODYSSEUS_PIPER_CONFIG", "")
            if config_path:
                cmd.extend(["--config", str(Path(config_path).expanduser())])

            voice = (voice_override or settings.get("tts_voice") or "").strip()
            if voice.isdigit():
                cmd.extend(["--speaker", voice])

            if speed and speed > 0:
                cmd.extend(["--length_scale", f"{1.0 / speed:.3f}"])

            proc = subprocess.run(
                cmd,
                input=text,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            if proc.returncode != 0:
                logger.error("Piper synthesis failed: %s", proc.stderr.strip() or proc.stdout.strip())
                return None

            return Path(tmp_path).read_bytes()
        except Exception as e:
            logger.error(f"Piper synthesis failed: {e}", exc_info=True)
            return None
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    def piper_catalog(self) -> list[dict]:
        settings = self._load_settings()
        voice_map = settings.get("tts_piper_voice_by_language") or {}
        rows = []
        for entry in PIPER_VOICE_CATALOG:
            item = dict(entry)
            item["model_url"] = f"{PIPER_VOICE_BASE}/{entry['path']}.onnx"
            item["config_url"] = f"{PIPER_VOICE_BASE}/{entry['path']}.onnx.json"
            item["model_path"] = str(self._piper_voices_dir(settings) / f"{entry['id']}.onnx")
            item["installed"] = Path(item["model_path"]).exists()
            item["assigned"] = voice_map.get(entry["language"]) == item["model_path"]
            rows.append(item)
        return rows

    def piper_voice_state(self) -> dict:
        settings = self._load_settings()
        return {
            "voices_dir": str(self._piper_voices_dir(settings)),
            "assigned": settings.get("tts_piper_voice_by_language") or {},
            "default_language": settings.get("tts_piper_default_language") or "en",
            "catalog": self.piper_catalog(),
        }

    def piper_catalog_entry(self, voice_id: str) -> Optional[dict]:
        for entry in self.piper_catalog():
            if entry["id"] == voice_id:
                return entry
        return None

    def assign_piper_voice(self, language: str, model_path: str, *, default_language: bool = False) -> dict:
        from src.settings import load_settings, save_settings

        lang = (language or "").lower().split("-", 1)[0].strip()
        if not lang:
            raise ValueError("language is required")
        path = str(Path(model_path).expanduser())
        if not Path(path).exists():
            raise ValueError(f"voice model does not exist: {path}")

        settings = load_settings()
        voice_map = settings.get("tts_piper_voice_by_language") or {}
        if not isinstance(voice_map, dict):
            voice_map = {}
        voice_map[lang] = path
        settings["tts_piper_voice_by_language"] = voice_map
        if default_language or not settings.get("tts_piper_default_language"):
            settings["tts_piper_default_language"] = lang
        if settings.get("tts_provider") == "piper" and lang == settings.get("tts_piper_default_language", "en"):
            settings["tts_model"] = path
        save_settings(settings)
        return self.piper_voice_state()

    def _detect_language(self, text: str, settings: dict) -> str:
        lowered = f" {text.lower()} "
        if any("Ѐ" <= ch <= "ӿ" for ch in text):
            return "ru"
        if any("一" <= ch <= "鿿" for ch in text):
            return "zh"

        scores = {
            "fr": [" le ", " la ", " les ", " des ", " une ", " est ", " bonjour", " français", " ça ", "être", " avec "],
            "es": [" el ", " la ", " los ", " una ", " que ", " estoy ", " hola", " español", " gracias", " para "],
            "de": [" der ", " die ", " das ", " und ", " ist ", " nicht ", " hallo", " deutsch", " ich "],
            "it": [" il ", " lo ", " la ", " una ", " che ", " ciao", " italiano", " grazie", " per "],
            "pt": [" o ", " a ", " os ", " uma ", " que ", " olá", " português", " obrigado", " para "],
            "nl": [" de ", " het ", " een ", " niet ", " hallo", " nederlands", " voor "],
            "pl": [" i ", " jest ", " nie ", " cześć", " polski", " dziękuję", " dla "],
            "en": [" the ", " and ", " is ", " are ", " hello", " english", " with ", " for "],
        }
        best_lang = settings.get("tts_piper_default_language") or "en"
        best_score = 0
        for lang, words in scores.items():
            score = sum(1 for word in words if word in lowered)
            if score > best_score:
                best_lang = lang
                best_score = score
        if any(ch in lowered for ch in "àâçéèêëîïôùûüÿœ"):
            best_lang = "fr"
        elif any(ch in lowered for ch in "áéíóúñ¿¡"):
            best_lang = "es"
        return best_lang

    def _split_language_segments(self, text: str, settings: dict) -> list[dict]:
        import re

        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
        if not parts:
            return []
        segments = []
        for part in parts:
            lang = self._detect_language(part, settings)
            if segments and segments[-1]["language"] == lang:
                segments[-1]["text"] += " " + part
            else:
                segments.append({"language": lang, "text": part})
        return segments

    def _concat_wavs(self, wavs: list[bytes]) -> Optional[bytes]:
        if not wavs:
            return None
        try:
            import audioop

            target_params = None
            frames_out = []
            target_rate = None
            target_channels = None
            target_width = None
            for data in wavs:
                with wave.open(io.BytesIO(data), "rb") as wf:
                    params = wf.getparams()
                    frames = wf.readframes(wf.getnframes())
                if target_params is None:
                    target_params = params
                    target_channels = params.nchannels
                    target_width = params.sampwidth
                    target_rate = params.framerate
                if params.nchannels != target_channels:
                    logger.warning("Skipping Piper segment with incompatible channel count")
                    continue
                if params.sampwidth != target_width:
                    logger.warning("Skipping Piper segment with incompatible sample width")
                    continue
                if params.framerate != target_rate:
                    frames, _ = audioop.ratecv(frames, params.sampwidth, params.nchannels, params.framerate, target_rate, None)
                frames_out.append(frames)
            if not frames_out or target_params is None:
                return None
            buf = io.BytesIO()
            with wave.open(buf, "wb") as out:
                out.setparams(target_params)
                out.writeframes(b"".join(frames_out))
            return buf.getvalue()
        except Exception as e:
            logger.error("Failed to concatenate Piper WAV segments: %s", e, exc_info=True)
            return None

    def _synthesize_piper_multilingual(self, text: str, settings: dict, speed: float = 1.0, model_override: str = "", voice_override: str = "") -> Optional[bytes]:
        voice_map = settings.get("tts_piper_voice_by_language") or {}
        if model_override or not isinstance(voice_map, dict) or not voice_map:
            return self._synthesize_piper(text, settings, speed, model_override=model_override, voice_override=voice_override)
        wavs = []
        for segment in self._split_language_segments(text, settings):
            wav = self._synthesize_piper(segment["text"], settings, speed, language=segment["language"], voice_override=voice_override)
            if wav:
                wavs.append(wav)
        return self._concat_wavs(wavs) if len(wavs) > 1 else (wavs[0] if wavs else None)

    def preview_piper_voice(self, voice_id: str, text: str = "") -> Optional[bytes]:
        entry = self.piper_catalog_entry(voice_id)
        if not entry:
            return None
        sample = text or entry.get("sample") or "Hello from Odysseus Voice Test."
        settings = self._load_settings()
        return self._synthesize_piper(sample, settings, _safe_speed(settings.get("tts_speed", "1")), model_override=entry["model_path"])

    # ── API endpoint ──

    def _synthesize_api(self, text: str, endpoint_id: str, model: str, voice: str, speed: float = 1.0) -> Optional[bytes]:
        from src.database import SessionLocal, ModelEndpoint

        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
            if not ep:
                logger.error(f"TTS endpoint {endpoint_id} not found")
                return None
            base_url = ep.base_url.rstrip("/")
            api_key = ep.api_key
        finally:
            db.close()

        url = base_url + "/audio/speech"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed,
        }

        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=60)
            r.raise_for_status()
            logger.info(f"API TTS: {len(r.content)} bytes from {base_url}")
            return r.content
        except Exception as e:
            logger.error(f"API TTS synthesis failed: {e}")
            return None

    # ── Public interface ──

    def _resolve_voice_config(self, settings: dict, profile_id: str = "") -> dict:
        """Merge the active voice profile (explicit id, or the default from
        settings) over the flat tts_* settings."""
        profile = self.get_voice_profile(profile_id or settings.get("tts_voice_profile") or "")
        provider = self._normalize_provider(settings["tts_provider"])
        cfg = {
            "provider": provider,
            "model": settings["tts_model"],
            "voice": settings["tts_voice"],
            "speed": _safe_speed(settings.get("tts_speed", "1")),
            "model_path": "",
            "ref_audio": "",
            "ref_text": "",
            "profile_id": "",
        }
        if profile:
            cfg["provider"] = self._normalize_provider(profile.get("provider") or provider)
            cfg["voice"] = profile.get("voice") or cfg["voice"]
            cfg["model_path"] = profile.get("model_path") or ""
            cfg["speed"] = _safe_speed(profile.get("speed"), cfg["speed"])
            cfg["ref_audio"] = profile.get("ref_audio") or ""
            cfg["ref_text"] = profile.get("ref_text") or ""
            cfg["profile_id"] = profile.get("id") or ""
        return cfg

    def _synthesize_with_provider(self, text: str, cfg: dict, settings: dict) -> Optional[bytes]:
        provider = cfg["provider"]
        if provider == "piper":
            return self._synthesize_piper_multilingual(
                text, settings, cfg["speed"],
                model_override=cfg["model_path"], voice_override=cfg["voice"],
            )
        if provider == "kokoro":
            kokoro = self._get_kokoro()
            model = cfg["model_path"] or settings.get("tts_kokoro_model") or "prince-canuma/Kokoro-82M"
            return kokoro.synthesize(text, voice=cfg["voice"] or "af_heart", speed=cfg["speed"], model=model)
        if provider == "dots_tts_mlx":
            dots = self._get_dots()
            model = cfg["model_path"] or settings.get("tts_dots_model") or ""
            return dots.synthesize(text, model=model, ref_audio=cfg["ref_audio"], ref_text=cfg["ref_text"], speed=cfg["speed"])
        if provider.startswith("endpoint:"):
            endpoint_id = provider.split(":", 1)[1]
            return self._synthesize_api(text, endpoint_id, cfg["model"], cfg["voice"], cfg["speed"])
        logger.error(f"Unknown TTS provider: {provider}")
        return None

    def synthesize(self, text: str, use_cache: bool = True, profile_id: str = "") -> Optional[bytes]:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False:
            return None
        cfg = self._resolve_voice_config(settings, profile_id)
        provider = cfg["provider"]

        if provider in ("disabled", "browser"):
            return None

        if len(text) > 5000:
            text = text[:5000]

        cache_model = cfg["model"]
        if provider == "piper" and not cfg["model_path"]:
            cache_model = json.dumps(settings.get("tts_piper_voice_by_language") or cfg["model"], sort_keys=True)
        elif cfg["model_path"]:
            cache_model = cfg["model_path"]
        # Profile id in the key ensures switching profiles never replays stale audio.
        cache_voice = f"{cfg['profile_id']}:{cfg['voice']}" if cfg["profile_id"] else cfg["voice"]

        key = self._cache_key(text, provider, cache_model, cache_voice, cfg["speed"])
        if use_cache:
            cached = self._get_cached(key)
            if cached:
                self.telemetry["cache_hits"] += 1
                logger.info(f"TTS cache hit ({len(text)} chars)")
                return cached

        started = time.monotonic()
        audio_data = self._synthesize_with_provider(text, cfg, settings)

        # Fallback tier: if the fancy provider fails mid-conversation, Piper
        # keeps the voice loop talking instead of going silent.
        if audio_data is None and provider not in ("piper",) and self._piper_available(settings):
            logger.warning("TTS provider %s failed — falling back to Piper", provider)
            self.telemetry["fallback_count"] += 1
            audio_data = self._synthesize_piper_multilingual(text, settings, cfg["speed"])

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if audio_data:
            duration = _wav_duration_seconds(audio_data)
            self.telemetry["synth_count"] += 1
            self.telemetry["last_provider"] = provider
            self.telemetry["last_latency_ms"] = elapsed_ms
            self.telemetry["last_audio_seconds"] = round(duration, 2)
            self.telemetry["last_rtf"] = round((elapsed_ms / 1000.0) / duration, 3) if duration else 0.0
            if use_cache:
                self._put_cache(key, audio_data)
        else:
            self.telemetry["error_count"] += 1

        return audio_data

    def preview_voice_profile(self, profile_id: str, text: str = "") -> Optional[bytes]:
        profile = self.get_voice_profile(profile_id)
        if not profile:
            return None
        sample = text or profile.get("sample_text") or "Hello! This is a preview of my voice."
        return self.synthesize(sample, use_cache=True, profile_id=profile_id)

    def synthesize_to_base64(self, text: str) -> Optional[str]:
        import base64
        audio = self.synthesize(text)
        if audio:
            return base64.b64encode(audio).decode("utf-8")
        return None

    def set_voice(self, voice: str):
        """Legacy no-op — voice is now managed via admin settings."""

    def get_stats(self) -> Dict[str, Any]:
        settings = self._load_settings()
        provider = self._effective_provider(settings)
        tts_enabled = settings.get("tts_enabled", True)

        cache_files = list(self.cache_dir.glob("*.wav")) + list(self.cache_dir.glob("*.mp3"))
        cache_size = sum(f.stat().st_size for f in cache_files)

        is_available = self.available and tts_enabled
        profile = self.get_voice_profile(settings.get("tts_voice_profile") or "")
        stats = {
            "available": is_available,
            "ready": is_available,
            "provider": provider,
            "model": settings["tts_model"],
            "voice": settings["tts_voice"],
            "speed": _safe_speed(settings.get("tts_speed", "1")),
            "voice_profile": profile["id"] if profile else "",
            "voice_profile_name": profile["name"] if profile else "",
            # Cache-busting key for client-side audio caches: changes whenever
            # the effective voice changes.
            "voice_key": f"{provider}|{profile['id'] if profile else settings['tts_voice']}",
            "cache_entries": len(cache_files),
            "cache_size_mb": round(cache_size / (1024 * 1024), 2),
            "telemetry": dict(self.telemetry),
        }
        if profile:
            stats["voice"] = profile.get("voice") or stats["voice"]
            stats["speed"] = _safe_speed(profile.get("speed"), stats["speed"])

        if provider == "kokoro":
            kokoro = self._get_kokoro()
            backend = kokoro.backend_name()
            stats["model"] = f"Kokoro-82M ({backend})" if backend else "Kokoro (runtime not installed)"
            if not kokoro.runtime_installed() and self._piper_available(settings):
                stats["model"] += " — using Piper fallback"
        elif provider == "piper":
            model = self._piper_model(settings)
            stats["model"] = model or "Piper model not configured"
            stats["ready"] = self._piper_available(settings)
            stats["available"] = stats["ready"] and tts_enabled
        elif provider == "dots_tts_mlx":
            stats["model"] = settings.get("tts_dots_model") or "dots.tts (not configured)"
        elif provider == "browser":
            stats["model"] = "Browser (Web Speech API)"
        elif provider.startswith("endpoint:"):
            stats["endpoint_id"] = provider.split(":", 1)[1]

        return stats


class _KokoroEngine:
    """Kokoro-82M with two interchangeable backends:

    1. mlx-audio (preferred on Apple Silicon — Metal via MLX)
    2. the `kokoro` torch package (CUDA → MPS → CPU)

    The model loads lazily on first synthesis; loading can take tens of
    seconds on a cold Hugging Face cache, so callers must run this in a
    threadpool, never on the event loop.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._mlx_model = None
        self._mlx_model_id = None
        self._torch_pipeline = None
        self._torch_device = None
        self._backend = None  # "mlx" | "torch" | None

    def runtime_installed(self) -> bool:
        import importlib.util
        return bool(
            importlib.util.find_spec("mlx_audio")
            or importlib.util.find_spec("kokoro")
        )

    def backend_name(self) -> str:
        if self._backend == "mlx":
            return "MLX / Apple Silicon"
        if self._backend == "torch":
            return f"torch / {self._torch_device}"
        import importlib.util
        if importlib.util.find_spec("mlx_audio"):
            return "MLX / Apple Silicon (not loaded yet)"
        if importlib.util.find_spec("kokoro"):
            return "torch (not loaded yet)"
        return ""

    # legacy attribute kept for anything poking at the old pipeline object
    @property
    def available(self) -> bool:
        return self.runtime_installed()

    def synthesize(self, text: str, voice: str = "af_heart", speed: float = 1.0, model: str = "prince-canuma/Kokoro-82M") -> Optional[bytes]:
        with self._lock:
            audio = self._synthesize_mlx(text, voice, speed, model)
            if audio is not None:
                return audio
            return self._synthesize_torch(text, voice, speed)

    # legacy signature used by old callers
    def synthesize_raw(self, text: str, voice: str = "af_heart") -> Optional[bytes]:
        return self.synthesize(text, voice=voice)

    def _synthesize_mlx(self, text: str, voice: str, speed: float, model: str) -> Optional[bytes]:
        try:
            import importlib.util
            if not importlib.util.find_spec("mlx_audio"):
                return None
            from mlx_audio.tts.utils import load_model

            if self._mlx_model is None or self._mlx_model_id != model:
                logger.info("Loading Kokoro MLX model: %s", model)
                self._mlx_model = load_model(model)
                self._mlx_model_id = model
                self._backend = "mlx"

            import numpy as np
            segments = []
            sample_rate = 24000
            for result in self._mlx_model.generate(text=text, voice=voice, speed=speed):
                seg = getattr(result, "audio", None)
                if seg is None:
                    continue
                sample_rate = int(getattr(result, "sample_rate", sample_rate) or sample_rate)
                segments.append(np.asarray(seg, dtype="float32").reshape(-1))
            if not segments:
                return None
            return _float_audio_to_wav(np.concatenate(segments), sample_rate)
        except Exception as e:
            logger.error("Kokoro MLX synthesis failed: %s", e, exc_info=True)
            # Drop the model so a corrupt load doesn't poison every later call
            self._mlx_model = None
            self._mlx_model_id = None
            return None

    def _synthesize_torch(self, text: str, voice: str, speed: float) -> Optional[bytes]:
        try:
            import importlib.util
            if not importlib.util.find_spec("kokoro"):
                return None
            import torch
            import numpy as np
            from kokoro import KPipeline

            if self._torch_pipeline is None:
                if torch.cuda.is_available():
                    device = "cuda:0"
                elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                    device = "mps"
                else:
                    device = "cpu"
                logger.info("Loading Kokoro torch pipeline on %s", device)
                self._torch_pipeline = KPipeline(lang_code="a")
                if hasattr(self._torch_pipeline, "model") and self._torch_pipeline.model is not None:
                    self._torch_pipeline.model = self._torch_pipeline.model.to(device)
                self._torch_device = device
                self._backend = "torch"

            chunks = []
            for _, _, audio in self._torch_pipeline(text, voice=voice, speed=speed):
                if hasattr(audio, "detach"):
                    audio = audio.detach().cpu().numpy()
                chunks.append(np.asarray(audio, dtype="float32").reshape(-1))
            if not chunks:
                return None
            return _float_audio_to_wav(np.concatenate(chunks), 24000)
        except Exception as e:
            logger.error("Kokoro torch synthesis failed: %s", e, exc_info=True)
            self._torch_pipeline = None
            return None


class _DotsTtsMlx:
    """Experimental dots.tts provider on Apple Silicon via mlx-audio.

    dots.tts is a 2B-parameter Apache-2.0 TTS model with high-quality
    zero-shot voice cloning. This provider loads a quantized MLX variant
    (settings key tts_dots_model, e.g. an mf-int4 repo from dots-tts-mlx)
    and passes optional consented reference audio for cloning. Everything is
    defensive: if the runtime or model is missing, we report a hint instead
    of erroring, and synthesis failures fall back to Piper upstream.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._model_id = None

    def runtime_installed(self) -> bool:
        import importlib.util
        return bool(importlib.util.find_spec("mlx_audio"))

    def configured(self, settings: dict) -> bool:
        return self.runtime_installed() and bool(settings.get("tts_dots_model"))

    def hint(self, settings: dict) -> str:
        if not self.runtime_installed():
            return "Install with: pip install mlx-audio, then set a dots.tts MLX model repo in settings (tts_dots_model)"
        if not settings.get("tts_dots_model"):
            return "Set tts_dots_model to a dots-tts-mlx repo (e.g. an mf-int4 quant) or local path"
        return ""

    def synthesize(self, text: str, model: str, ref_audio: str = "", ref_text: str = "", speed: float = 1.0) -> Optional[bytes]:
        if not model:
            logger.warning("dots.tts model not configured (settings key tts_dots_model)")
            return None
        with self._lock:
            try:
                from mlx_audio.tts.utils import load_model

                if self._model is None or self._model_id != model:
                    logger.info("Loading dots.tts MLX model: %s", model)
                    self._model = load_model(model)
                    self._model_id = model

                kwargs = {"text": text}
                if ref_audio and Path(ref_audio).expanduser().exists():
                    kwargs["ref_audio"] = str(Path(ref_audio).expanduser())
                    if ref_text:
                        kwargs["ref_text"] = ref_text
                if speed and speed != 1.0:
                    kwargs["speed"] = speed

                import numpy as np
                segments = []
                sample_rate = 24000
                try:
                    results = self._model.generate(**kwargs)
                except TypeError:
                    # Older/newer mlx-audio versions vary in accepted kwargs —
                    # retry with the minimal call before giving up.
                    kwargs.pop("speed", None)
                    results = self._model.generate(**kwargs)
                for result in results:
                    seg = getattr(result, "audio", None)
                    if seg is None:
                        continue
                    sample_rate = int(getattr(result, "sample_rate", sample_rate) or sample_rate)
                    segments.append(np.asarray(seg, dtype="float32").reshape(-1))
                if not segments:
                    return None
                return _float_audio_to_wav(np.concatenate(segments), sample_rate)
            except Exception as e:
                logger.error("dots.tts synthesis failed: %s", e, exc_info=True)
                self._model = None
                self._model_id = None
                return None


# Module-level singleton
_tts_service = None

def get_tts_service() -> TTSService:
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service
