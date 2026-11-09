# media_utils.py
import os
import json
import subprocess
import requests
from config import HEADERS
from ai_seo import sanitize_url


def inspect_video_file(filepath):
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-show_entries", "format=duration",
            "-of", "json",
            filepath
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        data = json.loads(res.stdout)
        streams = data.get("streams", [{}])
        width = int(streams[0].get("width", 1920))
        height = int(streams[0].get("height", 1080))
        dur_str = streams[0].get("duration") or data.get("format", {}).get("duration") or "0"
        duration = float(dur_str)
        return width, height, duration
    except Exception as e:
        print(f"[WARN] ffprobe error: {e}")
        return 1920, 1080, 120.0


def enhance_short_to_1080p(input_path, width, height):
    """Upscales low-res vertical Shorts to crisp 1080x1920 Full HD"""
    if width >= 1080 or height >= 1920:
        print(f"[QUALITY] Short is already Full HD ({width}x{height}).")
        return input_path

    enhanced_path = input_path.replace(".mp4", "_1080p.mp4")
    print(f"[QUALITY] Enhancing Short from {width}x{height} to 1080x1920 Full HD (Lanczos + Unsharp)...")
    vf_filter = (
        "scale=1080:1920:flags=lanczos:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        "unsharp=3:3:0.6:3:3:0.0"
    )
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", input_path,
        "-vf", vf_filter,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-b:v", "4500k",
        "-maxrate", "5500k",
        "-bufsize", "9000k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
        enhanced_path
    ]
    try:
        res = subprocess.run(cmd, timeout=180)
        if res.returncode == 0 and os.path.exists(enhanced_path) and os.path.getsize(enhanced_path) > 50_000:
            os.replace(enhanced_path, input_path)
            print("[QUALITY] Successfully upgraded Short to 1080x1920 Full HD.")
    except Exception as e:
        print(f"[WARN] Enhancement skipped ({e}), using original file.")
        if os.path.exists(enhanced_path):
            os.remove(enhanced_path)
    return input_path


def download_file(url, dest_path, min_bytes=3_000):
    clean_u = sanitize_url(url)
    if not clean_u:
        return False
    try:
        with requests.get(clean_u, headers=HEADERS, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        return os.path.exists(dest_path) and os.path.getsize(dest_path) > min_bytes
    except Exception as e:
        print(f"[WARN] Download failed for {clean_u}: {e}")
        return False


def prepare_clean_thumbnail(video_path, thumb_url, output_thumb_path, duration=120.0):
    """
    1. Downloads the official Footballi match poster.
    2. Upscales to 1280x720 HD using Lanczos, polishes colors and sharpens edges cleanly.
    3. ZERO artificial text, ZERO duplicate watermarks. 100% Free.
    """
    raw_thumb = output_thumb_path.replace(".jpg", "_raw.jpg")

    vf_enhance = (
        "scale=1280:720:flags=lanczos:force_original_aspect_ratio=increase,"
        "crop=1280:720,"
        "eq=contrast=1.07:saturation=1.12:brightness=0.01,"
        "unsharp=5:5:0.85:3:3:0.0"
    )

    if thumb_url and download_file(thumb_url, raw_thumb, min_bytes=3_000):
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", raw_thumb, "-vf", vf_enhance, "-q:v", "2", output_thumb_path]
        subprocess.run(cmd, timeout=30)
        if os.path.exists(raw_thumb):
            os.remove(raw_thumb)
        if os.path.exists(output_thumb_path) and os.path.getsize(output_thumb_path) > 8_000:
            print("[THUMBNAIL] Official Footballi poster polished to 1280x720 HD.")
            return output_thumb_path

    if os.path.exists(raw_thumb):
        os.remove(raw_thumb)

    # Fallback to clean video frame only if website had no poster at all
    if video_path and os.path.exists(video_path):
        seek_sec = max(5.0, min(duration * 0.25, 45.0))
        cmd_frame = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(seek_sec),
            "-i", video_path,
            "-vf", f"thumbnail=25,{vf_enhance}",
            "-frames:v", "1",
            "-q:v", "2",
            output_thumb_path
        ]
        subprocess.run(cmd_frame, timeout=35)
        if os.path.exists(output_thumb_path) and os.path.getsize(output_thumb_path) > 8_000:
            return output_thumb_path

    return None
