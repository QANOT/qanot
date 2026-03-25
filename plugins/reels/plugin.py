"""Reels Plugin — AI-powered Instagram Reel generator.

Telegram dan: "QR-kod haqida reel yasa" → tayyor MP4 video.

Pipeline:
1. GPT-5.4 → ssenariy (kirill + lotin)
2. ElevenLabs → voiceover (voice clone + timestamps)
3. Pexels → b-roll footage (kontentga mos)
4. Pillow → word-by-word captionlar
5. FFmpeg → compose (video + audio + SFX + music)
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import shutil
import base64
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from qanot.plugins.base import Plugin, ToolDef, tool

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
ASSETS_DIR = PLUGIN_DIR / "assets"
OUTPUT_DIR = PLUGIN_DIR / "output"


# ═══════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════

@dataclass
class Word:
    text: str
    start_s: float
    end_s: float
    _force_emphasis: bool = False

    @property
    def is_emphasis(self) -> bool:
        if self._force_emphasis:
            return True
        clean = self.text.strip("!?.,")
        return clean.isupper() and len(clean) > 1


@dataclass
class Scene:
    name: str
    text: str
    start_s: float
    end_s: float
    footage_query: str
    footage_path: str = ""
    zoom_style: str = "zoom-in"
    words: list[Word] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


# ═══════════════════════════════════════════
# TRANSLITERATION
# ═══════════════════════════════════════════

_CYR2LAT = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'j',
    'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
    'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'x','ц':'ts',
    'ч':'ch','ш':'sh','ъ':'','ы':'i','ь':'','э':'e','ю':'yu',
    'я':'ya','ў':"o'",'қ':'q','ғ':"g'",'ҳ':'h',
    'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'E','Ё':'Yo','Ж':'J',
    'З':'Z','И':'I','Й':'Y','К':'K','Л':'L','М':'M','Н':'N','О':'O',
    'П':'P','Р':'R','С':'S','Т':'T','У':'U','Ф':'F','Х':'X',
    'Ч':'Ch','Ш':'Sh','Ъ':'','Ы':'I','Ь':'','Э':'E','Ю':'Yu',
    'Я':'Ya','Ў':"O'",'Қ':'Q','Ғ':"G'",'Ҳ':'H',
}

def _cyr2lat(text: str) -> str:
    return "".join(_CYR2LAT.get(c, c) for c in text)


# ═══════════════════════════════════════════
# PLUGIN
# ═══════════════════════════════════════════

class ReelsPlugin(Plugin):
    name = "reels"
    description = "AI-powered Instagram Reel generator"
    version = "1.0.0"
    tools_md = (PLUGIN_DIR / "TOOLS.md").read_text() if (PLUGIN_DIR / "TOOLS.md").exists() else ""
    soul_append = (PLUGIN_DIR / "SOUL_APPEND.md").read_text() if (PLUGIN_DIR / "SOUL_APPEND.md").exists() else ""

    def __init__(self):
        self.elevenlabs_key = ""
        self.elevenlabs_voice_id = ""
        self.pexels_key = ""
        self.openai_key = ""

    async def setup(self, config: dict) -> None:
        self.elevenlabs_key = config.get("elevenlabs_key", "")
        self.elevenlabs_voice_id = config.get("elevenlabs_voice_id", "")
        self.pexels_key = config.get("pexels_key", "")
        self.openai_key = config.get("openai_key", "")

        # Ensure directories
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (ASSETS_DIR / "sfx").mkdir(exist_ok=True)
        (ASSETS_DIR / "music").mkdir(exist_ok=True)
        (ASSETS_DIR / "footage").mkdir(exist_ok=True)
        (ASSETS_DIR / "fonts").mkdir(exist_ok=True)

        # Download Montserrat font if missing
        font_path = ASSETS_DIR / "fonts" / "Montserrat-ExtraBold.ttf"
        if not font_path.exists():
            try:
                urllib.request.urlretrieve(
                    "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf",
                    str(font_path),
                )
            except Exception:
                pass

        # Generate basic SFX if missing
        self._ensure_sfx()
        self._ensure_music()

    def _ensure_sfx(self):
        sfx_dir = ASSETS_DIR / "sfx"
        for name, cmd in [
            ("whoosh.wav", "anoisesrc=d=0.4:c=pink -af afade=t=in:d=0.08,afade=t=out:st=0.12:d=0.28,highpass=f=200,lowpass=f=5000,volume=4"),
            ("impact.wav", "sine=f=50:d=0.4 -af afade=t=in:d=0.02,afade=t=out:st=0.08:d=0.32,volume=5"),
            ("ding.wav", "sine=f=1047:d=0.4 -af afade=t=out:st=0.1:d=0.3,volume=3"),
        ]:
            path = sfx_dir / name
            if not path.exists():
                subprocess.run(
                    f"ffmpeg -y -f lavfi -i {cmd} {path}",
                    shell=True, capture_output=True, timeout=10,
                )

    def _ensure_music(self):
        music_dir = ASSETS_DIR / "music"
        if not list(music_dir.glob("*.mp3")):
            try:
                raw = music_dir / "raw.mp3"
                urllib.request.urlretrieve("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-3.mp3", str(raw))
                subprocess.run([
                    "ffmpeg", "-y", "-i", str(raw), "-t", "25",
                    "-af", "afade=t=in:d=1,afade=t=out:st=22:d=3,volume=0.35",
                    str(music_dir / "bg.mp3"),
                ], capture_output=True, timeout=30)
                raw.unlink(missing_ok=True)
            except Exception:
                pass

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="create_reel",
                description="Instagram Reel yaratish. Mavzu bering — tayyor MP4 video qaytaradi. Ssenariy, voiceover, footage, caption, musiqa, SFX — hammasi avtomatik.",
                parameters={
                    "type": "object",
                    "required": ["topic"],
                    "properties": {
                        "topic": {"type": "string", "description": "Reel mavzusi — masalan: 'QR-kod 2026 dan majburiy'"},
                    },
                },
                handler=self._create_reel,
            ),
        ]

    async def _create_reel(self, params: dict) -> str:
        topic = params.get("topic", "")
        if not topic:
            return json.dumps({"error": "topic kerak"})

        try:
            result = await asyncio.to_thread(self._pipeline, topic)
            return json.dumps({
                "success": True,
                "video_path": str(result),
                "message": f"Reel tayyor: {result.name}",
            })
        except Exception as e:
            logger.exception("Reel creation failed")
            return json.dumps({"error": str(e)})

    def _pipeline(self, topic: str) -> Path:
        """Full pipeline: topic → finished MP4."""
        work_dir = OUTPUT_DIR / "_work"
        work_dir.mkdir(parents=True, exist_ok=True)

        # 1. Script
        script = self._generate_script(topic)

        # 2. Voiceover
        vo_path = work_dir / "voiceover.mp3"
        words = self._generate_voiceover(script["voiceover_cyrillic"], vo_path)

        # Mark emphasis
        emphasis = {e.lower() for e in script.get("emphasis_words", [])}
        for w in words:
            if w.text.lower().rstrip("!?,.'") in emphasis:
                w._force_emphasis = True

        # 3. Scenes
        scenes = []
        for s in script["scenes"]:
            scene = Scene(
                name=s["name"], text=s["text_latin"],
                start_s=s["start_s"], end_s=s["end_s"],
                footage_query=s["footage_query"],
                zoom_style=s.get("zoom_style", "zoom-in"),
            )
            scene.words = [w for w in words if w.start_s >= scene.start_s and w.start_s < scene.end_s]
            scenes.append(scene)

        # 4. Footage
        self._fetch_footage(scenes, script["title"])

        # 5. Captions
        duration = words[-1].end_s + 0.5
        caption_dir = work_dir / "_captions"
        self._render_captions(words, duration, caption_dir)

        # 6. Compose
        final = self._compose(script["title"], scenes, vo_path, caption_dir, words)

        # Cleanup
        shutil.rmtree(work_dir, ignore_errors=True)

        return final

    def _generate_script(self, topic: str) -> dict:
        """GPT-5.4 ssenariy yozadi."""
        system = """Sen @tadbirkor.ai Instagram Reel uchun ssenariy yozasan. O'zbek tilida.
QOIDALAR: 20-30 soniya. Kirill harfda voiceover. 5-7 scene. Qisqa energetik gaplar.
RAQAMLAR SO'Z BILAN: "2026" → "икки минг йигирма олтинчи". "1%" → "бир фоиз".
footage_query: FAQAT 2-3 so'z biznes kontekst. Har scene BOSHQA.
JSON: {"title":"..","mood":"urgent|upbeat|calm|neutral","voiceover_cyrillic":"..","voiceover_latin":"..","emphasis_words":["JARIMA","XATO"],"scenes":[{"name":"01_hook","text_latin":"..","start_s":0,"end_s":3,"footage_query":"businessman desk","zoom_style":"zoom-in"}]}"""

        payload = json.dumps({
            "model": "gpt-5.4",
            "temperature": 0.7,
            "max_completion_tokens": 3000,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Mavzu: {topic}"},
            ],
            "response_format": {"type": "json_object"},
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {self.openai_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())

        return json.loads(data["choices"][0]["message"]["content"])

    def _generate_voiceover(self, text_cyrillic: str, output_path: Path) -> list[Word]:
        """ElevenLabs voiceover + word timestamps."""
        payload = json.dumps({
            "text": text_cyrillic,
            "model_id": "eleven_v3",
            "voice_settings": {"stability": 0.35, "similarity_boost": 0.8, "style": 0.75, "speed": 1.05, "use_speaker_boost": True},
            "language_code": "tr",
        }).encode()

        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.elevenlabs_voice_id}/with-timestamps",
            data=payload,
            headers={"xi-api-key": self.elevenlabs_key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())

        output_path.write_bytes(base64.b64decode(data["audio_base64"]))

        # Extract words
        chars = data["alignment"]["characters"]
        starts = data["alignment"]["character_start_times_seconds"]
        ends = data["alignment"]["character_end_times_seconds"]

        words = []
        cur, ws = "", 0.0
        for i, ch in enumerate(chars):
            if ch == " " or i == len(chars) - 1:
                if i == len(chars) - 1 and ch != " ":
                    cur += ch
                clean = cur.strip()
                if clean and not (clean.startswith("[") and clean.endswith("]")):
                    words.append(Word(text=_cyr2lat(clean), start_s=round(ws, 3), end_s=round(ends[max(0, i-1)], 3)))
                cur = ""
                if i + 1 < len(starts):
                    ws = starts[i + 1]
            else:
                if not cur:
                    ws = starts[i]
                cur += ch

        return words

    def _fetch_footage(self, scenes: list[Scene], title: str):
        """Pexels footage yuklab olish."""
        footage_dir = ASSETS_DIR / "footage" / title
        footage_dir.mkdir(parents=True, exist_ok=True)

        for scene in scenes:
            dest = footage_dir / f"{scene.name}.mp4"
            if dest.exists() and dest.stat().st_size > 10000:
                scene.footage_path = str(dest)
                continue

            query = "+".join((scene.footage_query + " business professional office").split()[:5])
            url = f"https://api.pexels.com/videos/search?query={query}&orientation=portrait&size=medium&per_page=5"
            req = urllib.request.Request(url, headers={"Authorization": self.pexels_key, "User-Agent": "QanotReels/1.0"})

            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                for video in data.get("videos", []):
                    for f in video["video_files"]:
                        if f.get("height", 0) > f.get("width", 0) and f.get("width", 0) >= 720:
                            subprocess.run(["curl", "-sL", "-o", str(dest), f["link"]], capture_output=True, timeout=30)
                            if dest.exists() and dest.stat().st_size > 10000:
                                scene.footage_path = str(dest)
                            break
                    if scene.footage_path:
                        break
            except Exception:
                pass

    def _render_captions(self, words: list[Word], duration: float, output_dir: Path):
        """Pillow word-by-word caption renderer."""
        from PIL import Image, ImageDraw, ImageFont

        output_dir.mkdir(parents=True, exist_ok=True)
        font_path = ASSETS_DIR / "fonts" / "Montserrat-ExtraBold.ttf"
        font = ImageFont.truetype(str(font_path), 64) if font_path.exists() else ImageFont.load_default()

        W, H = 1080, 1920
        pages = [words[i:i+4] for i in range(0, len(words), 4)]

        for frame_num in range(int(duration * 30)):
            t = frame_num / 30
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # Find active page
            page = None
            for p in pages:
                if t >= p[0].start_s - 0.1 and t < p[-1].end_s + 0.15:
                    page = p
                    break

            if page:
                space = font.getbbox("  ")[2]
                infos = []
                for w in page:
                    bbox = font.getbbox(w.text)
                    infos.append({"word": w, "w": bbox[2]-bbox[0], "active": t >= w.start_s and t < w.end_s, "past": t >= w.end_s})

                total_w = sum(i["w"] + space for i in infos) - space
                x = (W - total_w) // 2
                y = int(H * 0.65)

                for info in infos:
                    w, is_active, is_past = info["word"], info["active"], info["past"]
                    if is_active:
                        draw.rounded_rectangle([x-12, y-8, x+info["w"]+12, y+int(64*1.15)+8], radius=10, fill=(255, 215, 0, 240))
                        draw.text((x, y), w.text, font=font, fill=(255, 255, 255, 255))
                    else:
                        for dx in (-2, 0, 2):
                            for dy in (0, 2, 4):
                                draw.text((x+dx, y+dy), w.text, font=font, fill=(0, 0, 0, 220))
                        draw.text((x, y), w.text, font=font, fill=(255, 255, 255, 255) if is_past else (200, 200, 200, 200))
                    x += info["w"] + space

            img.save(output_dir / f"caption_{frame_num:05d}.png")

    def _compose(self, title: str, scenes: list[Scene], vo_path: Path, caption_dir: Path, words: list[Word]) -> Path:
        """FFmpeg compose — video + captions + audio."""
        work_dir = OUTPUT_DIR / "_work"

        # Prepare scene clips
        clips = []
        color_filter = "colorbalance=rs=0.05:gs=0.02:bs=-0.03,eq=brightness=0.02:contrast=1.05:saturation=0.92"
        for i, scene in enumerate(scenes):
            if not scene.footage_path:
                continue
            clip = work_dir / f"scene_{i:02d}.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-i", scene.footage_path, "-t", str(scene.duration_s),
                "-vf", f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1",
                "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-r", "30",
                str(clip),
            ], capture_output=True, timeout=60)
            if clip.exists():
                clips.append(clip)

        if not clips:
            raise RuntimeError("No footage")

        # Concat
        concat = work_dir / "concat.mp4"
        list_file = work_dir / "list.txt"
        list_file.write_text("\n".join(f"file '{c}'" for c in clips))
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-vf", color_filter, "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-r", "30",
            str(concat),
        ], capture_output=True, timeout=120)

        # Overlay captions + vignette
        captioned = work_dir / "captioned.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-i", str(concat), "-framerate", "30",
            "-i", str(caption_dir / "caption_%05d.png"),
            "-filter_complex", "[0:v]vignette=angle=PI/4:aspect=9/16[vig];[vig][1:v]overlay=0:0:shortest=1",
            "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-r", "30",
            str(captioned),
        ], capture_output=True, timeout=120)

        # Mix audio
        final = OUTPUT_DIR / f"{title}.mp4"
        music = next(iter((ASSETS_DIR / "music").glob("*.mp3")), None)

        inputs = ["-i", str(captioned), "-i", str(vo_path)]
        filt = f"[1:a]volume=1.0[vo]"
        mix = "[vo]"
        count = 1

        if music:
            inputs += ["-i", str(music)]
            filt += f";[2:a]aloop=loop=-1:size=2e+09,atrim=duration=60,afade=t=out:st=55:d=5,volume=0.6[bg]"
            mix = "[vo][bg]"
            count = 2

        if count > 1:
            filt += f";{mix}amix=inputs={count}:duration=shortest[aout]"
        else:
            filt += ";[vo]acopy[aout]"

        subprocess.run([
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filt,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
            str(final),
        ], capture_output=True, timeout=120)

        return final
