"""Provider layer tests: voice profiles, provider selection, fallback, caching."""
import io
import wave

import pytest

from services.tts.tts_service import TTSService, _safe_speed, _wav_duration_seconds


BASE_SETTINGS = {
    "tts_enabled": True,
    "tts_provider": "disabled",
    "tts_model": "tts-1",
    "tts_voice": "alloy",
    "tts_speed": "1",
    "tts_piper_voice_by_language": {},
    "tts_piper_default_language": "en",
    "tts_piper_voices_dir": "",
    "tts_voice_profile": "",
    "tts_kokoro_model": "prince-canuma/Kokoro-82M",
    "tts_dots_model": "",
}


def make_service(tmp_path, monkeypatch, **overrides):
    svc = TTSService(cache_dir=str(tmp_path / "cache"), voices_file=str(tmp_path / "voices.json"))
    settings = dict(BASE_SETTINGS)
    settings.update(overrides)
    monkeypatch.setattr(svc, "_load_settings", lambda: dict(settings))
    return svc


def make_wav(seconds: float = 0.5, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class FakeKokoro:
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    def runtime_installed(self):
        return True

    def backend_name(self):
        return "fake"

    def synthesize(self, text, voice="af_heart", speed=1.0, model=""):
        self.calls += 1
        return self.result


# ── Voice profiles ──

def test_save_list_get_update_voice_profile(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)
    stored = svc.save_voice_profile({"name": "Heart", "provider": "kokoro", "voice": "af_heart"})
    assert stored["id"]
    assert svc.get_voice_profile(stored["id"])["voice"] == "af_heart"
    assert len(svc.list_voice_profiles()) == 1

    # Update in place — same id, no duplicate
    svc.save_voice_profile({"id": stored["id"], "name": "Heart", "provider": "kokoro", "voice": "af_bella"})
    profiles = svc.list_voice_profiles()
    assert len(profiles) == 1
    assert profiles[0]["voice"] == "af_bella"


def test_save_profile_validation(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        svc.save_voice_profile({"name": "", "provider": "kokoro"})
    with pytest.raises(ValueError):
        svc.save_voice_profile({"name": "x", "provider": "nonsense"})
    # Cloned voice without a consent note is rejected
    with pytest.raises(ValueError):
        svc.save_voice_profile({
            "name": "clone", "provider": "dots_tts_mlx",
            "ref_audio": "/tmp/ref.wav", "consent_note": "",
        })
    # "local" legacy alias is normalized to kokoro
    stored = svc.save_voice_profile({"name": "legacy", "provider": "local"})
    assert stored["provider"] == "kokoro"


def test_delete_voice_profile(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)
    stored = svc.save_voice_profile({"name": "Heart", "provider": "kokoro"})
    # Deleting clears settings default via src.settings — stub it out
    monkeypatch.setattr("src.settings.load_settings", lambda: {"tts_voice_profile": ""})
    monkeypatch.setattr("src.settings.save_settings", lambda s: None)
    assert svc.delete_voice_profile(stored["id"]) is True
    assert svc.delete_voice_profile(stored["id"]) is False
    assert svc.list_voice_profiles() == []


# ── Provider resolution ──

def test_local_alias_normalizes_to_kokoro():
    assert TTSService._normalize_provider("local") == "kokoro"
    assert TTSService._normalize_provider("piper") == "piper"


def test_profile_overrides_flat_settings(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, tts_provider="piper")
    stored = svc.save_voice_profile({
        "name": "Fast Bella", "provider": "kokoro", "voice": "af_bella", "speed": 1.3,
    })
    settings = svc._load_settings()
    settings["tts_voice_profile"] = stored["id"]
    cfg = svc._resolve_voice_config(settings)
    assert cfg["provider"] == "kokoro"
    assert cfg["voice"] == "af_bella"
    assert cfg["speed"] == 1.3
    assert cfg["profile_id"] == stored["id"]


def test_disabled_and_browser_return_none(tmp_path, monkeypatch):
    for provider in ("disabled", "browser"):
        svc = make_service(tmp_path, monkeypatch, tts_provider=provider)
        assert svc.synthesize("hello world") is None


def test_tts_enabled_false_blocks_synthesis(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, tts_provider="piper", tts_enabled=False)
    assert svc.synthesize("hello") is None


# ── Synthesis, fallback, cache ──

def test_kokoro_synthesis_and_cache_hit(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, tts_provider="kokoro")
    fake = FakeKokoro(result=make_wav())
    monkeypatch.setattr(svc, "_get_kokoro", lambda: fake)

    first = svc.synthesize("hello there")
    assert first == fake.result
    assert svc.telemetry["synth_count"] == 1
    assert svc.telemetry["last_provider"] == "kokoro"
    assert svc.telemetry["last_audio_seconds"] > 0

    second = svc.synthesize("hello there")
    assert second == fake.result
    assert fake.calls == 1  # served from cache
    assert svc.telemetry["cache_hits"] == 1


def test_kokoro_failure_falls_back_to_piper(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, tts_provider="kokoro")
    monkeypatch.setattr(svc, "_get_kokoro", lambda: FakeKokoro(result=None))
    piper_wav = make_wav(0.2)
    monkeypatch.setattr(svc, "_piper_available", lambda settings: True)
    monkeypatch.setattr(svc, "_synthesize_piper_multilingual", lambda *a, **kw: piper_wav)

    out = svc.synthesize("fall back please", use_cache=False)
    assert out == piper_wav
    assert svc.telemetry["fallback_count"] == 1


def test_all_providers_fail_counts_error(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, tts_provider="kokoro")
    monkeypatch.setattr(svc, "_get_kokoro", lambda: FakeKokoro(result=None))
    monkeypatch.setattr(svc, "_piper_available", lambda settings: False)
    assert svc.synthesize("nope", use_cache=False) is None
    assert svc.telemetry["error_count"] == 1


def test_cache_key_varies_with_voice_profile_and_speed(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)
    k1 = svc._cache_key("hi", "kokoro", "m", "p1:af_heart", 1.0)
    k2 = svc._cache_key("hi", "kokoro", "m", "p2:af_heart", 1.0)
    k3 = svc._cache_key("hi", "kokoro", "m", "p1:af_heart", 1.5)
    assert len({k1, k2, k3}) == 3


def test_stats_voice_key_tracks_profile(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, tts_provider="kokoro")
    monkeypatch.setattr(svc, "_get_kokoro", lambda: FakeKokoro(result=None))
    stats_before = svc.get_stats()

    stored = svc.save_voice_profile({"name": "Heart", "provider": "kokoro", "voice": "af_heart"})
    svc2 = make_service(tmp_path, monkeypatch, tts_provider="kokoro", tts_voice_profile=stored["id"])
    monkeypatch.setattr(svc2, "_get_kokoro", lambda: FakeKokoro(result=None))
    stats_after = svc2.get_stats()

    assert stats_before["voice_key"] != stats_after["voice_key"]
    assert stats_after["voice_profile_name"] == "Heart"
    assert "telemetry" in stats_after


# ── Helpers ──

def test_safe_speed_still_defensive():
    assert _safe_speed("fast") == 1.0
    assert _safe_speed("") == 1.0
    assert _safe_speed("-2") == 1.0
    assert _safe_speed("1.5") == 1.5


def test_wav_duration_parses_and_tolerates_garbage():
    assert abs(_wav_duration_seconds(make_wav(0.5)) - 0.5) < 0.01
    assert _wav_duration_seconds(b"ID3 not a wav") == 0.0
