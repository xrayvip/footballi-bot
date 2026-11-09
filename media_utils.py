# media_utils.py
import os
import re
import json
import subprocess
import requests
from config import HEADERS
from ai_seo import sanitize_url

try:
    from PIL import Image, ImageDraw, ImageFont, ImageEnhance
    import arabic_reshaper
    from bidi.algorithm import get_display
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


def shape_persian(text):
    """Correctly reshape and reorder Persian RTL text for Pillow rendering"""
    if not text:
        return ""
    try:
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text


def load_persian_font(size):
    """Find best installed Persian/Arabic bold font on Ubuntu runner or download Vazirmatn"""
    font_candidates = [
        "/usr/share/fonts/truetype/vazirmatn/Vazirmatn-ExtraBold.ttf",
        "/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Bold.ttf",
        "/tmp/Vazirmatn-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ]
    for fp in font_candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue

    # Download Vazirmatn-Bold if not found locally
    try:
        font_url = "https" + "://github" + ".com/rastikerdar/vazirmatn/raw/master/fonts/ttf/Vazirmatn-ExtraBold.ttf"
        r = requests.get(font_url, timeout=15)
        if r.status_code == 200:
            with open("/tmp/Vazirmatn-Bold.ttf", "wb") as f:
                f.write(r.content)
            return ImageFont.truetype("/tmp/Vazirmatn-Bold.ttf", size)
    except Exception:
        pass

    return ImageFont.load_default()


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


def inspect_image_dimensions(img_path):
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            img_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(res.stdout)
        streams = data.get("streams", [{}])
        return int(streams[0].get("width", 0)), int(streams[0].get("height", 0))
    except Exception:
        return 0, 0


def render_branded_overlay(image_path, headline_text, is_summary=False):
    """
    Applies Footballi's official emerald/gold news agency graphic template onto the 1280x720 image:
    - Cinematic dark gradient on the lower 45%
    - Emerald/Gold accent bar at the bottom
    - Top/Bottom category badge ('خلاصه بازی' or 'فوتبالی | ویدیو اختصاصی')
    - Large, high-contrast Persian headline with black stroke shadow
    """
    if not PIL_AVAILABLE or not os.path.exists(image_path):
        return

    try:
        img = Image.open(image_path).convert("RGBA")
        img = img.resize((1280, 720), Image.Resampling.LANCZOS)

        # Slightly boost color & contrast of base photo
        img = ImageEnhance.Color(img).enhance(1.15)
        img = ImageEnhance.Contrast(img).enhance(1.08)

        overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # 1. Smooth dark gradient from y=340 down to y=720
        for y in range(340, 720):
            progress = (y - 340) / (720 - 340)
            alpha = int(235 * (progress ** 1.3))
            draw.line([(0, y), (1280, y)], fill=(6, 18, 14, alpha))

        # 2. Bottom emerald-green brand bar (14px height)
        draw.rectangle([(0, 706), (1280, 720)], fill=(0, 185, 105, 255))

        # 3. Top-right official badge ("FOOTBALLI")
        badge_font = load_persian_font(30)
        top_badge_text = shape_persian("رسانه رسمی فوتبالی")
        draw.rounded_rectangle([(960, 32), (1235, 88)], radius=12, fill=(8, 28, 22, 220), outline=(0, 195, 110, 255), width=2)
        draw.text((1097, 60), top_badge_text, font=badge_font, fill=(255, 255, 255, 255), anchor="mm")

        # 4. Category tag pill above the main headline
        cat_label = "خلاصه مسابقه" if is_summary else "گزارش و حواشی"
        cat_color = (0, 175, 95, 245) if is_summary else (215, 38, 56, 245)
        cat_shaped = shape_persian(cat_label)
        cat_font = load_persian_font(32)

        pill_w = 240
        draw.rounded_rectangle(
            [(1280 - 60 - pill_w, 495), (1280 - 60, 550)],
            radius=10,
            fill=cat_color
        )
        draw.text((1280 - 60 - pill_w // 2, 522), cat_shaped, font=cat_font, fill=(255, 255, 255, 255), anchor="mm")

        # 5. Main Headline Typography (auto-fit font size based on length)
        clean_hl = re.sub(r"#\S+", "", headline_text or "").strip()
        if len(clean_hl) > 38:
            clean_hl = clean_hl[:36].rsplit(" ", 1)[0] + "..."

        font_size = 66 if len(clean_hl) <= 24 else 54
        title_font = load_persian_font(font_size)
        shaped_title = shape_persian(clean_hl)

        # Gold-yellow text for summaries, crisp white for interviews/news
        text_color = (255, 225, 65, 255) if is_summary else (255, 255, 255, 255)

        # Draw heavy black stroke for 100% readability on mobile screens
        x_pos = 1220
        y_pos = 625
        for dx in range(-4, 5, 2):
            for dy in range(-4, 5, 2):
                if dx != 0 or dy != 0:
                    draw.text((x_pos + dx, y_pos + dy), shaped_title, font=title_font, fill=(0, 0, 0, 240), anchor="rm")

        draw.text((x_pos, y_pos), shaped_title, font=title_font, fill=text_color, anchor="rm")

        final_img = Image.alpha_composite(img, overlay).convert("RGB")
        final_img.save(image_path, "JPEG", quality=94)
        print(f"[THUMBNAIL] Branded news agency overlay rendered with text: '{clean_hl}'")
    except Exception as e:
        print(f"[WARN] Overlay rendering note: {e}")


def build_hd_thumbnail(video_path, thumb_url, output_thumb_path, duration=120.0, headline_text="", is_summary=False):
    """
    Creates a 1280x720 HD Branded Thumbnail:
    1. Gets high-res background (from web poster if >=700px, otherwise extracts sharpest frame from video).
    2. Renders Footballi's official dark-emerald gradient, category badge, and bold Persian headline.
    """
    raw_thumb = output_thumb_path.replace(".jpg", "_raw.jpg")
    use_video_frame = True

    if thumb_url and download_file(thumb_url, raw_thumb, min_bytes=8_000):
        w, h = inspect_image_dimensions(raw_thumb)
        if w >= 700 and h >= 390:
            use_video_frame = False

    vf_scale = (
        "scale=1280:720:flags=lanczos:force_original_aspect_ratio=increase,"
        "crop=1280:720,"
        "unsharp=5:5:0.7:3:3:0.0"
    )

    if not use_video_frame:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", raw_thumb, "-vf", vf_scale, "-q:v", "2", output_thumb_path]
        subprocess.run(cmd, timeout=30)
    else:
        seek_sec = max(5.0, min(duration * 0.25, 45.0))
        cmd_frame = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(seek_sec),
            "-i", video_path,
            "-vf", f"thumbnail=30,{vf_scale}",
            "-frames:v", "1",
            "-q:v", "2",
            output_thumb_path
        ]
        subprocess.run(cmd_frame, timeout=45)

    if os.path.exists(raw_thumb):
        os.remove(raw_thumb)

    if os.path.exists(output_thumb_path) and os.path.getsize(output_thumb_path) > 10_000:
        if headline_text:
            render_branded_overlay(output_thumb_path, headline_text, is_summary=is_summary)
        return output_thumb_path

    return None


def enhance_short_to_1080p(input_path, width, height):
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


def download_file(url, dest_path, min_bytes=50_000):
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
