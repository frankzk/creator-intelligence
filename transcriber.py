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


def transcribe_words(media_path: str, language: str = "es") -> tuple[str, list[dict]]:
    """Transcribe with word-level timestamps (accepts video files directly).

    Returns (transcript, words) where words is a list of
    {"text": str, "start": float, "end": float} dicts. Empty list for
    clips without voice (b-roll), which downstream renders as no captions.
    """
    model = _get_model()
    segments, _ = model.transcribe(
        media_path, beam_size=5, language=language,
        word_timestamps=True, vad_filter=True,
    )
    parts: list[str] = []
    words: list[dict] = []
    for seg in segments:
        parts.append(seg.text.strip())
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"text": text, "start": round(w.start, 3), "end": round(w.end, 3)})
    return " ".join(parts).strip(), words
