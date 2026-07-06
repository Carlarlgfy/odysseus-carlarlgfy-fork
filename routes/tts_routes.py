# routes/tts_routes.py
"""
TTS API routes — multi-provider (Kokoro, Piper, dots.tts, API endpoint, browser).
"""

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel
import logging
from pathlib import Path
import httpx

logger = logging.getLogger(__name__)

class TTSRequest(BaseModel):
    text: str
    format: str = "audio"  # "audio" or "base64"

class PiperDownloadRequest(BaseModel):
    voice_id: str

class PiperAssignRequest(BaseModel):
    language: str
    model_path: str
    default_language: bool = False

class PiperPreviewRequest(BaseModel):
    voice_id: str
    text: str = ""

class VoiceProfileRequest(BaseModel):
    id: str = ""
    name: str
    provider: str
    voice: str = ""
    model_path: str = ""
    speed: float = 1.0
    sample_text: str = ""
    consent_note: str = ""
    source: str = ""
    ref_audio: str = ""
    ref_text: str = ""
    set_default: bool = False

class VoicePreviewRequest(BaseModel):
    profile_id: str
    text: str = ""

class VoiceDefaultRequest(BaseModel):
    profile_id: str = ""  # empty = clear default (use flat tts_* settings)


def _audio_response(audio_data: bytes, filename: str = "speech") -> Response:
    # Detect format from magic bytes (MP3: ID3 tag or sync word ff e0+)
    is_mp3 = audio_data[:3] == b'ID3' or (len(audio_data) >= 2 and audio_data[0] == 0xff and (audio_data[1] & 0xe0) == 0xe0)
    mime = "audio/mpeg" if is_mp3 else "audio/wav"
    ext = "mp3" if is_mp3 else "wav"
    return Response(
        content=audio_data,
        media_type=mime,
        headers={"Content-Disposition": f"inline; filename={filename}.{ext}"},
    )


def setup_tts_routes(tts_service):
    """Setup TTS routes with the provided TTS service"""
    router = APIRouter(prefix="/api/tts", tags=["tts"])

    @router.get("/stats")
    async def get_tts_stats():
        """Get TTS service statistics"""
        try:
            return tts_service.get_stats()
        except Exception as e:
            logger.error(f"Failed to get TTS stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/synthesize")
    async def synthesize_speech(request: TTSRequest):
        """Synthesize speech from text"""
        try:
            if not tts_service.available:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "TTS service not available"}
                )

            if request.format == "base64":
                # Blocking, CPU/GPU-bound (Piper subprocess, Kokoro inference) —
                # must not run on the event loop or it stalls chat streaming.
                audio_b64 = await run_in_threadpool(tts_service.synthesize_to_base64, request.text)
                if not audio_b64:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                return {"audio": audio_b64}

            else:  # audio format
                audio_data = await run_in_threadpool(tts_service.synthesize, request.text)
                if not audio_data:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                return _audio_response(audio_data)

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Synthesis error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": f"Synthesis failed: {str(e)}"}
            )

    @router.post("/clear-cache")
    async def clear_tts_cache():
        """Clear TTS cache"""
        try:
            tts_service.clear_cache()
            return {"success": True, "message": "Cache cleared"}
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Providers ──

    @router.get("/providers")
    async def list_providers():
        """Provider tiers with install/availability status for the Voice Library."""
        try:
            return {"providers": tts_service.list_providers()}
        except Exception as e:
            logger.error(f"Failed to list TTS providers: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    # ── Voice profiles ──

    @router.get("/voices")
    async def list_voices():
        """Saved voice profiles plus which one is the default."""
        try:
            return {"voices": tts_service.list_voice_profiles()}
        except Exception as e:
            logger.error(f"Failed to list voice profiles: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/voices")
    async def save_voice(request: VoiceProfileRequest):
        """Create or update a voice profile; optionally set it as default."""
        try:
            profile = tts_service.save_voice_profile(request.model_dump())
            if request.set_default:
                tts_service.set_default_voice_profile(profile["id"])
            return profile
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"message": str(e)})
        except Exception as e:
            logger.error(f"Failed to save voice profile: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/voices/{profile_id}")
    async def delete_voice(profile_id: str):
        try:
            if not tts_service.delete_voice_profile(profile_id):
                raise HTTPException(status_code=404, detail={"message": "Voice profile not found"})
            return {"success": True}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete voice profile: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/voices/default")
    async def set_default_voice(request: VoiceDefaultRequest):
        try:
            tts_service.set_default_voice_profile(request.profile_id)
            return {"success": True, "profile_id": request.profile_id}
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"message": str(e)})
        except Exception as e:
            logger.error(f"Failed to set default voice: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/voices/preview")
    async def preview_voice(request: VoicePreviewRequest):
        """Synthesize a short sample with a saved voice profile."""
        try:
            audio_data = await run_in_threadpool(
                tts_service.preview_voice_profile, request.profile_id, request.text
            )
        except Exception as e:
            logger.error(f"Voice preview failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail={"message": f"Preview failed: {str(e)}"})
        if not audio_data:
            raise HTTPException(status_code=400, detail={"message": "Preview failed. Check the provider is installed and configured."})
        return _audio_response(audio_data, filename="voice-preview")

    # ── Piper voice catalog (kept for compatibility) ──

    @router.get("/piper/voices")
    async def piper_voices():
        """Return curated Piper voice catalog plus installed/assigned state."""
        try:
            return tts_service.piper_voice_state()
        except Exception as e:
            logger.error(f"Failed to get Piper voices: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/piper/download")
    async def download_piper_voice(request: PiperDownloadRequest):
        """Download a Piper voice model and config into the configured voices dir."""
        entry = tts_service.piper_catalog_entry(request.voice_id)
        if not entry:
            raise HTTPException(status_code=404, detail={"message": "Voice not found"})

        model_path = Path(entry["model_path"])
        config_path = Path(str(model_path) + ".json")
        model_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                for url, dest in ((entry["model_url"], model_path), (entry["config_url"], config_path)):
                    if dest.exists() and dest.stat().st_size > 0:
                        continue
                    resp = await client.get(url)
                    resp.raise_for_status()
                    dest.write_bytes(resp.content)
            return tts_service.piper_voice_state()
        except Exception as e:
            logger.error(f"Failed to download Piper voice {request.voice_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail={"message": f"Download failed: {str(e)}"})

    @router.post("/piper/assign")
    async def assign_piper_voice(request: PiperAssignRequest):
        """Assign an installed Piper voice path to a language code."""
        try:
            return tts_service.assign_piper_voice(
                request.language,
                request.model_path,
                default_language=request.default_language,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"message": str(e)})
        except Exception as e:
            logger.error(f"Failed to assign Piper voice: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/piper/preview")
    async def preview_piper_voice(request: PiperPreviewRequest):
        """Preview an installed Piper voice."""
        audio_data = await run_in_threadpool(
            tts_service.preview_piper_voice, request.voice_id, request.text
        )
        if not audio_data:
            raise HTTPException(status_code=400, detail={"message": "Preview failed. Download this voice first."})
        return Response(
            content=audio_data,
            media_type="audio/wav",
            headers={"Content-Disposition": "inline; filename=piper-preview.wav"},
        )

    return router
