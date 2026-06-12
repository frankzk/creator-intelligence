import os
import yt_dlp


def _make_ydl_opts(extra: dict = None) -> dict:
    base = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }
    if extra:
        base.update(extra)
    return base


def scrape_profile_metadata(url: str, max_videos: int = 25) -> list[dict]:
    """
    Fast metadata-only scrape for a TikTok creator profile.
    Returns list of video dicts without downloading any media.
    """
    opts = _make_ydl_opts({
        "extract_flat": True,
        "playlistend": max_videos,
        "skip_download": True,
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise RuntimeError(f"No info returned for {url}")

    entries = info.get("entries", [info] if info.get("id") else [])

    videos = []
    for entry in entries:
        if not entry:
            continue
        tiktok_id = entry.get("id", "")
        if not tiktok_id:
            continue
        videos.append({
            "tiktok_id": tiktok_id,
            "title": entry.get("title") or entry.get("description") or "",
            "views": int(entry.get("view_count") or 0),
            "likes": int(entry.get("like_count") or 0),
            "comments": int(entry.get("comment_count") or 0),
            "shares": int(entry.get("repost_count") or 0),
            "duration": int(entry.get("duration") or 0),
            "url": entry.get("webpage_url") or entry.get("url") or "",
        })

    return videos


def scrape_profile_full(url: str, max_videos: int = 25) -> list[dict]:
    """
    Full scrape: downloads full metadata (not audio) for each video.
    Needed when extract_flat misses some fields.
    """
    opts = _make_ydl_opts({
        "extract_flat": False,
        "playlistend": max_videos,
        "skip_download": True,
        "writeinfojson": False,
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise RuntimeError(f"No info returned for {url}")

    entries = info.get("entries", [info] if info.get("id") else [])

    videos = []
    for entry in entries:
        if not entry:
            continue
        tiktok_id = entry.get("id", "")
        if not tiktok_id:
            continue
        videos.append({
            "tiktok_id": tiktok_id,
            "title": entry.get("title") or entry.get("description") or "",
            "views": int(entry.get("view_count") or 0),
            "likes": int(entry.get("like_count") or 0),
            "comments": int(entry.get("comment_count") or 0),
            "shares": int(entry.get("repost_count") or 0),
            "duration": int(entry.get("duration") or 0),
            "url": entry.get("webpage_url") or entry.get("url") or "",
        })

    return videos


def download_video_audio(video_url: str, output_dir: str) -> str:
    """
    Download just the audio from a TikTok video.
    Returns path to the .mp3 file.
    """
    os.makedirs(output_dir, exist_ok=True)
    outtmpl = os.path.join(output_dir, "%(id)s.%(ext)s")

    opts = _make_ydl_opts({
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        # Video único: que el error real se propague (geo-bloqueo, privado,
        # extractor desactualizado) en vez de devolver None en silencio.
        "ignoreerrors": False,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "96",
        }],
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)

    video_id = info.get("id", "")
    mp3_path = os.path.join(output_dir, f"{video_id}.mp3")

    if not os.path.exists(mp3_path):
        # yt-dlp may use the original ext then convert — look for any audio file with that id
        for f in os.listdir(output_dir):
            if f.startswith(video_id):
                return os.path.join(output_dir, f)
        raise FileNotFoundError(f"Audio file not found for video {video_id}")

    return mp3_path


def extract_username_from_url(url: str) -> str:
    """Extract @username from a TikTok profile URL."""
    url = url.rstrip("/")
    part = url.split("/")[-1]
    return part.lstrip("@")
