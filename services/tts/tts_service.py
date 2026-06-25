# src/tts_service.py
"""Multi-provider TTS service — dispatches to Piper, Kokoro, API, or browser."""

import io
import wave
import logging
import hashlib
import httpx
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any

from src.constants import DATA_DIR, TTS_CACHE_DIR

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


class TTSService:
    """Multi-provider TTS service.

    Reads provider config from data/settings.json on each call.
    Providers:
      "disabled"        — no TTS
      "browser"         — client-side Web Speech API (no server synthesis)
      "piper"           — Piper CLI using a local .onnx voice model
      "local"           — Kokoro-82M on GPU (legacy local provider)
      "endpoint:<id>"   — OpenAI-compatible /audio/speech via ModelEndpoint
    """

    def __init__(self, cache_dir: str = TTS_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._kokoro = None  # lazy-init

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
        }

    @property
    def available(self) -> bool:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False:
            return False
        provider = settings["tts_provider"]
        if provider == "disabled":
            return False
        if provider == "browser":
            return True  # handled client-side
        if provider == "piper":
            return self._piper_available(settings)
        if provider == "local":
            kokoro = self._get_kokoro()
            return kokoro is not None and kokoro.available
        if provider.startswith("endpoint:"):
            return True  # assume reachable; errors surface at synthesis time
        return False

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

    # ── Kokoro (local) ──

    def _get_kokoro(self):
        if self._kokoro is None:
            self._kokoro = _KokoroPipeline()
        return self._kokoro

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

    def _synthesize_piper(self, text: str, settings: dict, speed: float = 1.0, language: str = "", model_override: str = "") -> Optional[bytes]:
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

            voice = (settings.get("tts_voice") or "").strip()
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
        if any("\u0400" <= ch <= "\u04ff" for ch in text):
            return "ru"
        if any("\u4e00" <= ch <= "\u9fff" for ch in text):
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

    def _synthesize_piper_multilingual(self, text: str, settings: dict, speed: float = 1.0) -> Optional[bytes]:
        voice_map = settings.get("tts_piper_voice_by_language") or {}
        if not isinstance(voice_map, dict) or not voice_map:
            return self._synthesize_piper(text, settings, speed)
        wavs = []
        for segment in self._split_language_segments(text, settings):
            wav = self._synthesize_piper(segment["text"], settings, speed, language=segment["language"])
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

    def synthesize(self, text: str, use_cache: bool = True) -> Optional[bytes]:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False:
            return None
        provider = settings["tts_provider"]
        model = settings["tts_model"]
        voice = settings["tts_voice"]
        speed = _safe_speed(settings.get("tts_speed", "1"))

        if provider in ("disabled", "browser"):
            return None

        if len(text) > 5000:
            text = text[:5000]

        cache_model = model
        if provider == "piper":
            cache_model = json.dumps(settings.get("tts_piper_voice_by_language") or model, sort_keys=True)

        if use_cache:
            key = self._cache_key(text, provider, cache_model, voice, speed)
            cached = self._get_cached(key)
            if cached:
                logger.info(f"TTS cache hit ({len(text)} chars)")
                return cached

        audio_data = None

        if provider == "piper":
            audio_data = self._synthesize_piper_multilingual(text, settings, speed)
        elif provider == "local":
            kokoro = self._get_kokoro()
            if kokoro and kokoro.available:
                audio_data = kokoro.synthesize_raw(text, voice)
            else:
                logger.warning("Kokoro TTS not available")
                return None
        elif provider.startswith("endpoint:"):
            endpoint_id = provider.split(":", 1)[1]
            audio_data = self._synthesize_api(text, endpoint_id, model, voice, speed)
        else:
            logger.error(f"Unknown TTS provider: {provider}")
            return None

        if audio_data and use_cache:
            key = self._cache_key(text, provider, cache_model, voice, speed)
            self._put_cache(key, audio_data)

        return audio_data

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
        provider = settings["tts_provider"]
        tts_enabled = settings.get("tts_enabled", True)

        cache_files = list(self.cache_dir.glob("*.wav")) + list(self.cache_dir.glob("*.mp3"))
        cache_size = sum(f.stat().st_size for f in cache_files)

        is_available = self.available and tts_enabled
        stats = {
            "available": is_available,
            "ready": is_available,
            "provider": provider,
            "model": settings["tts_model"],
            "voice": settings["tts_voice"],
            "speed": _safe_speed(settings.get("tts_speed", "1")),
            "cache_entries": len(cache_files),
            "cache_size_mb": round(cache_size / (1024 * 1024), 2),
        }

        if provider == "local":
            kokoro = self._get_kokoro()
            stats["model"] = "Kokoro-82M (GPU)" if (kokoro and kokoro.available) else "Kokoro (not loaded)"
        elif provider == "piper":
            model = self._piper_model(settings)
            stats["model"] = model or "Piper model not configured"
            stats["ready"] = self._piper_available(settings)
            stats["available"] = stats["ready"] and tts_enabled
        elif provider == "browser":
            stats["model"] = "Browser (Web Speech API)"
        elif provider.startswith("endpoint:"):
            stats["endpoint_id"] = provider.split(":", 1)[1]

        return stats


class _KokoroPipeline:
    """Encapsulates the Kokoro-82M local GPU pipeline."""

    def __init__(self):
        self.pipeline = None
        self.available = False
        self.device = None
        self._init()

    def _init(self):
        try:
            import torch
            from kokoro import KPipeline

            if not torch.cuda.is_available():
                logger.warning("CUDA not available for Kokoro TTS")
                return

            self.device = torch.device("cuda:0")
            with torch.cuda.device(0):
                self.pipeline = KPipeline(lang_code="a")
                if hasattr(self.pipeline, "model"):
                    self.pipeline.model = self.pipeline.model.to(self.device)
            self.available = True
            logger.info("Kokoro-82M TTS pipeline loaded")
        except ImportError as e:
            logger.warning(f"Kokoro TTS not available: {e}")
            logger.warning("Install with: pip install kokoro soundfile")
        except Exception as e:
            logger.error(f"Kokoro init failed: {e}", exc_info=True)

    def synthesize_raw(self, text: str, voice: str = "af_heart") -> Optional[bytes]:
        if not self.available:
            return None
        try:
            import torch
            import numpy as np

            with torch.cuda.device(self.device):
                chunks = []
                for _, _, audio in self.pipeline(text, voice=voice):
                    chunks.append(audio)

            if not chunks:
                return None

            full = np.concatenate(chunks)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes((full * 32767).astype(np.int16).tobytes())
            return buf.getvalue()
        except Exception as e:
            logger.error(f"Kokoro synthesis failed: {e}", exc_info=True)
            return None


# Module-level singleton
_tts_service = None

def get_tts_service() -> TTSService:
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service
