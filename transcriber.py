import os
from faster_whisper import WhisperModel

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel("base", device="cpu", compute_type="int8")
    return _model


def transcribe_audio(audio_path: str) -> str:
    """Transcribe an audio file and return the full transcript as a string."""
    model = _get_model()
    segments, _ = model.transcribe(audio_path, beam_size=5, language="es")
    return " ".join(seg.text.strip() for seg in segments).strip()


def transcribe_and_cleanup(audio_path: str) -> str:
    """Transcribe the file, delete it afterward, return transcript."""
    try:
        return transcribe_audio(audio_path)
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)
