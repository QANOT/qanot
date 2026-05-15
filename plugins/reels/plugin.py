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
from typing import Any

from qanot.plugins.base import Plugin, ToolDef, tool

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
ASSETS_DIR = PLUGIN_DIR / "assets"
OUTPUT_DIR = PLUGIN_DIR / "output"


# ═══════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════

class Word:
    def __init__(self, text: str, start_s: float, end_s: float):
        self.text = text
        self.start_s = start_s
        self.end_s = end_s
        self.force_emphasis = False

    @property
    def is_emphasis(self) -> bool:
        if self.force_emphasis:
            return True
        clean = self.text.strip("!?.,")
        return clean.isupper() and len(clean) > 1


class Scene:
    def __init__(self, name: str, text: str, start_s: float, end_s: float,
                 footage_query: str, footage_path: str = "", zoom_style: str = "zoom-in"):
        self.name = name
        self.text = text
        self.start_s = start_s
        self.end_s = end_s
        self.footage_query = footage_query
        self.footage_path = footage_path
        self.zoom_style = zoom_style
        self.words: list[Word] = []

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
        self._scriptwriter_skill = ""

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

        # Load scriptwriter skill (research-backed framework injected into LLM prompt)
        skill_path = PLUGIN_DIR / "skills" / "REEL_SCRIPTWRITER.md"
        if skill_path.exists():
            self._scriptwriter_skill = skill_path.read_text(encoding="utf-8")

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
        music_dir.mkdir(exist_ok=True)
        for mood in ("urgent", "upbeat", "calm", "neutral"):
            (music_dir / mood).mkdir(exist_ok=True)
        if not list(music_dir.rglob("*.mp3")):
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
                w.force_emphasis = True

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

        # Beat-synced scene timing — per-mood BPM, scenes snap to beat grid.
        # Replaces LLM-supplied timings (which drift from real VO) with rhythmic
        # cuts that match the music. LLM scene queries are kept; cycled if too few.
        mood_for_bpm = script.get("mood", "neutral")
        bpm = {"urgent": 130, "upbeat": 120, "calm": 90, "neutral": 100}.get(mood_for_bpm, 100)
        beat_dur = 60.0 / bpm
        total_vo_dur = (words[-1].end_s + 0.3) if words else 30.0
        beats_per_scene = max(2, round(2.2 / beat_dur))
        scene_dur = beats_per_scene * beat_dur
        n_scenes = max(1, int(total_vo_dur / scene_dur))

        src_scenes = list(scenes) or [Scene(
            name="00_default", text="", start_s=0, end_s=scene_dur,
            footage_query="business professional", zoom_style="zoom-in",
        )]
        scenes = []
        for i in range(n_scenes):
            t_start = i * scene_dur
            t_end = min((i + 1) * scene_dur, total_vo_dur)
            src = src_scenes[i % len(src_scenes)]
            new_scene = Scene(
                name=f"{i:02d}_shot",
                text=src.text,
                start_s=t_start, end_s=t_end,
                footage_query=src.footage_query,
                zoom_style=src.zoom_style,
            )
            new_scene.words = [w for w in words if t_start <= w.start_s < t_end]
            scenes.append(new_scene)

        # 4. Footage
        self._fetch_footage(scenes, script["title"])

        # 5. Compose via HyperFrames (replaces ffmpeg + Pillow caption pipeline).
        # Captions, transitions, audio mix, and final encode all happen in
        # the HTML composition rendered by `hyperframes render`.
        final = self._compose_via_hyperframes(
            title=script["title"],
            scenes=scenes,
            vo_path=vo_path,
            words=words,
            mood=script.get("mood", "neutral"),
        )

        # Cleanup
        shutil.rmtree(work_dir, ignore_errors=True)

        return final

    def _generate_script(self, topic: str) -> dict:
        """GPT-5.4 ssenariy yozadi."""
        skill_block = self._scriptwriter_skill or ""
        system = f"""You are a content creator for @tadbirkor.ai — a VALUE-FIRST Uzbek entrepreneur channel.

CRITICAL: This is NOT a Qanot AI ad channel. 80% of content is PURE VALUE (storytelling, lifehack, personal development, education) with NO product mention. Only 10% of reels are direct Qanot product. The audience subscribes because the channel is USEFUL, not because it sells.

Apply the framework below RIGOROUSLY. Default to value-first content. Pick ONE category per script (most likely: storytelling or lifehack, NOT product reel).

═══════════ EMBEDDED SKILL ═══════════
{skill_block}
═══════════ END SKILL ═══════════

OUTPUT FORMAT (must match exactly — JSON only):
{{
  "title": "Latin Uzbek title, max 8 words",
  "mood": "urgent|upbeat|calm|neutral",
  "framework": "PAS|HPSC|BAB|HORMOZI",
  "voiceover_cyrillic": "STRICT: 50-60 Uzbek words MAX. At 150 wpm this reads as 20-26 seconds. Numbers as words.",
  "voiceover_latin": "Same content in Latin Uzbek for reference",
  "emphasis_words": ["UPPERCASE words for TTS volume bump, max 5"],
  "self_score": {{"hook":2,"specificity":2,"pain":2,"proof":2,"cta":2,"density":2,"caption_quality":2,"audio_plan":2,"length":2,"uzbek_authenticity":2,"total":20}},
  "scenes": [
    {{"name":"01_hook","section":"HOOK|PAIN|AGITATE|SOLUTION|PROOF|CTA","text_latin":"phrase chunk","start_s":0,"end_s":2,"footage_query":"2-3 word Pexels query","zoom_style":"zoom-in|zoom-out|pan"}}
  ]
}}

🔴 HARD CONSTRAINTS (rejection if violated):

1. **CONTENT CATEGORY**: Pick ONE per script. Distribution target across all reels:
   - 40% **storytelling** (real biznesmen voqealari — failure→recovery, lessons)
   - 20% **lifehack** (productivity, kassir nazorat, mijoz xizmat trick)
   - 15% **shaxsiy_rivojlanish** (mindset, odat, lider sifatlari)
   - 15% **industry_educational** (soliq yangilik, marketing, sotish psikologiya)
   - 10% **direct_product** (Qanot AI demo — only when explicit product topic)

   For most user requests, DEFAULT to storytelling or lifehack. Only choose direct_product when user EXPLICITLY asks for "Qanot reklamasi" or product demo.

2. **CTA RULES** (must match category):
   - storytelling/lifehack/personal_dev → engagement CTA ("Saqla, do'stga yubor", "Komment yoz: sizda ham shumi?")
   - industry_educational → save/channel CTA ("Saqlang, kerak bo'ladi", "@tadbirkor_ai kanalida har kuni")
   - direct_product → Qanot CTA ("@qanotai_bot — 7 kun bepul")
   - **NEVER** put Qanot product CTA on a storytelling/lifehack reel — kills value perception

3. **DURATION**: voiceover_cyrillic MUST be **50-60 Uzbek words MAXIMUM** (20-26 seconds at 150 wpm). >65 words = retention crash, REWRITE shorter.

4. **SCENE COUNT**: 10-13 scenes. Each scene 1.5-2.5s.

5. **SPECIFICITY**: Hook (scene 1) MUST have name+number+timeframe (rule of 3) — even for value content. Generic = scroll.

6. **VERTICAL JARGON**: Default = universal Uzbek SMB voice. Use vertical terms (MChJ, e-Faktura, Soliq.uz, BHM, NDS, 1C) ONLY in industry_educational or direct_product reels when topic requires it. Otherwise: "do'kon", "biznes", "sotuv", "kassir", "mijoz" universal vocabulary.

7. **NO KALKA**: Zero English-Uzbek calque. Banned: "1 tap", "real-time", "boost", "scale", "ROI", "click below", "subscribe now".

8. **SELF-SCORE**: total must be >=15/22. If <15, REWRITE before returning.

9. **WORD COUNT VALIDATION**: Count voiceover_cyrillic words BEFORE returning. If >60, cut content.

10. **CHARACTER DIVERSITY**: NEVER reuse "Komilxon aka" or "Akmal aka" — those are skill examples, NOT defaults. For EACH new script, pick a DIFFERENT name + location + business type from the pool in section 2.5. Random combinations: "Sherzod aka — Andijonda non sotuvchi", "Mohira opa — Yunusobodda kosmetika", "Davron aka — Samarqandda restoran". The skill examples are showing patterns; you must INVENT a new persona per script.

OUTPUT: Add `"content_category"` field to JSON: "storytelling" | "lifehack" | "shaxsiy_rivojlanish" | "industry_educational" | "direct_product"
"""

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

        script = json.loads(data["choices"][0]["message"]["content"])

        # Hard length validation — IG Reel optimal zone is 20-26s.
        # If LLM over-produced, retry once with stricter prompt; otherwise truncate.
        vo = script.get("voiceover_cyrillic", "")
        word_count = len(vo.split())
        if word_count > 65:
            logger.warning("[reels] voiceover too long (%d words, want <=60). Retrying with stricter prompt.", word_count)
            retry_user = (
                f"Mavzu: {topic}\n\n"
                f"⚠️ AVVALGI URINISH XATO: voiceover_cyrillic {word_count} so'z bo'ldi. "
                f"MAX 60 SO'Z. Qaytadan yoz, qisqaroq, faqat eng kuchli gaplar qoldir."
            )
            payload = json.dumps({
                "model": "gpt-5.4",
                "temperature": 0.5,
                "max_completion_tokens": 3000,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": retry_user},
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
            script = json.loads(data["choices"][0]["message"]["content"])
            vo = script.get("voiceover_cyrillic", "")
            word_count = len(vo.split())
            logger.info("[reels] retry produced %d words", word_count)

        return script

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
        """Pexels footage — group scenes by query, distribute different candidates
        per scene index so repeated queries don't yield identical clips."""
        footage_dir = ASSETS_DIR / "footage" / title
        footage_dir.mkdir(parents=True, exist_ok=True)

        from collections import defaultdict
        groups: dict[str, list[Scene]] = defaultdict(list)
        for s in scenes:
            groups[s.footage_query].append(s)

        for query, group in groups.items():
            q = "+".join((query + " business professional office").split()[:5])
            url = f"https://api.pexels.com/videos/search?query={q}&orientation=portrait&size=medium&per_page=10"
            req = urllib.request.Request(url, headers={"Authorization": self.pexels_key, "User-Agent": "QanotReels/1.0"})
            candidates: list[str] = []
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                for video in data.get("videos", []):
                    for f in video["video_files"]:
                        if f.get("height", 0) > f.get("width", 0) and f.get("width", 0) >= 720:
                            candidates.append(f["link"])
                            break
            except Exception:
                pass

            for i, scene in enumerate(group):
                dest = footage_dir / f"{scene.name}.mp4"
                if dest.exists() and dest.stat().st_size > 10000:
                    scene.footage_path = str(dest)
                    continue
                if not candidates:
                    continue
                link = candidates[i % len(candidates)]
                tmp = dest.with_suffix(".raw.mp4")
                # Trim to slightly more than scene duration to keep clip lightweight
                # (Chromium loads full video into memory; long clips kill RAM).
                trim_dur = max(2.5, scene.duration_s + 1.0)
                try:
                    subprocess.run(["curl", "-sL", "-o", str(tmp), link], capture_output=True, timeout=30)
                    if not (tmp.exists() and tmp.stat().st_size > 10000):
                        continue
                    # Re-encode to Chromium-compatible H.264 + trim to scene length.
                    # Pexels profiles vary; baseline + yuv420p decodes reliably in <video>.
                    re_enc = subprocess.run([
                        "ffmpeg", "-y", "-i", str(tmp),
                        "-t", str(round(trim_dur, 2)),
                        "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
                        "-pix_fmt", "yuv420p", "-r", "30", "-g", "30", "-keyint_min", "30",
                        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1",
                        "-movflags", "+faststart", "-an",
                        str(dest),
                    ], capture_output=True, timeout=45)
                    if re_enc.returncode == 0 and dest.exists() and dest.stat().st_size > 10000:
                        scene.footage_path = str(dest)
                    else:
                        # Fallback: keep raw download if re-encode failed
                        tmp.replace(dest)
                        scene.footage_path = str(dest)
                    if tmp.exists():
                        try: tmp.unlink()
                        except Exception: pass
                except Exception as e:
                    logger.warning("[reels] footage fetch failed for scene %d: %s", i, e)


    def _render_captions(self, words: list[Word], duration: float, output_dir: Path):
        """Phrase-chunk caption renderer — 2-3 words per chunk, no pill,
        white text + drop shadow + black stroke, auto-shrink on overflow."""
        from PIL import Image, ImageDraw, ImageFont

        output_dir.mkdir(parents=True, exist_ok=True)
        font_path = ASSETS_DIR / "fonts" / "Montserrat-ExtraBold.ttf"
        BASE_FONT = 76
        font = ImageFont.truetype(str(font_path), BASE_FONT) if font_path.exists() else ImageFont.load_default()

        W, H = 1080, 1920
        MAX_W = int(W * 0.85)
        Y_POS = int(H * 0.72)
        SHADOW_OFFSET = 5
        STROKE = 3
        total_frames = int(duration * 30)
        tail_grace = 0.2

        empty_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        empty_img.save(output_dir / "empty.png")

        if not words:
            for frame_num in range(total_frames):
                fname = f"caption_{frame_num:05d}.png"
                if not (output_dir / fname).exists():
                    (output_dir / fname).symlink_to("empty.png")
            return

        # Build phrase chunks: 2-3 words, break on punctuation or width
        space_w = font.getbbox(" ")[2]
        chunks: list[list[Word]] = []
        current: list[Word] = []
        current_w = 0
        for w in words:
            ww = font.getbbox(w.text)[2] - font.getbbox(w.text)[0]
            proj_w = current_w + ww + (space_w if current else 0)
            ends_phrase = bool(w.text and w.text[-1] in ",.!?")
            if current and (proj_w > MAX_W or len(current) >= 3):
                chunks.append(current)
                current = [w]
                current_w = ww
            else:
                current.append(w)
                current_w = proj_w
            if ends_phrase and current:
                chunks.append(current)
                current = []
                current_w = 0
        if current:
            chunks.append(current)

        chunk_cache: dict[int, str] = {}
        last_end = words[-1].end_s

        for frame_num in range(total_frames):
            t = frame_num / 30
            fname = f"caption_{frame_num:05d}.png"

            active_chunk = -1
            for ci, ch in enumerate(chunks):
                if t >= ch[0].start_s and t < ch[-1].end_s + 0.15:
                    active_chunk = ci
                    break

            if active_chunk < 0 or t > last_end + tail_grace:
                if not (output_dir / fname).exists():
                    (output_dir / fname).symlink_to("empty.png")
                continue

            if active_chunk in chunk_cache:
                src = chunk_cache[active_chunk]
                if not (output_dir / fname).exists():
                    try:
                        (output_dir / fname).symlink_to(src)
                    except OSError:
                        import shutil
                        shutil.copy2(output_dir / src, output_dir / fname)
                continue

            text = " ".join(w.text.rstrip(",.!?:;") for w in chunks[active_chunk])

            # Auto-shrink for overflow safety
            f = font
            bbox = f.getbbox(text)
            text_w = bbox[2] - bbox[0]
            if text_w > MAX_W:
                scale = MAX_W / text_w
                new_size = max(48, int(BASE_FONT * scale * 0.95))
                f = ImageFont.truetype(str(font_path), new_size) if font_path.exists() else ImageFont.load_default()
                bbox = f.getbbox(text)
                text_w = bbox[2] - bbox[0]

            x = (W - text_w) // 2 - bbox[0]
            y = Y_POS

            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.text((x + SHADOW_OFFSET, y + SHADOW_OFFSET), text, font=f, fill=(0, 0, 0, 200))
            draw.text((x, y), text, font=f, fill=(255, 255, 255, 255),
                      stroke_width=STROKE, stroke_fill=(0, 0, 0, 230))

            img.save(output_dir / fname)
            chunk_cache[active_chunk] = fname

    def _compose(self, title: str, scenes: list[Scene], vo_path: Path, caption_dir: Path, words: list[Word], mood: str = "neutral") -> Path:
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
        # Pick music: mood subdir first, then root, random within set
        import random
        music_root = ASSETS_DIR / "music"
        mood_dir = music_root / (mood if mood in {"urgent", "upbeat", "calm", "neutral"} else "neutral")
        candidates = list(mood_dir.glob("*.mp3")) if mood_dir.exists() else []
        if not candidates:
            candidates = list(music_root.glob("*.mp3"))
        music = random.choice(candidates) if candidates else None

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
            filt += f";{mix}amix=inputs={count}:duration=shortest[mixed]"
        else:
            filt += ";[vo]acopy[mixed]"
        filt += ";[mixed]loudnorm=I=-14:TP=-1.0:LRA=11[aout]"

        subprocess.run([
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filt,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
            str(final),
        ], capture_output=True, timeout=120)

        return final

    # ═══════════════════════════════════════════════════════════════
    # HyperFrames-based composer (replaces ffmpeg+Pillow pipeline above)
    # ═══════════════════════════════════════════════════════════════

    def _build_caption_chunks_hf(self, words: list[Word]) -> list[dict]:
        """Group words into 2-3 word phrase chunks with non-overlapping timing.
        Each chunk ends BEFORE the next one starts so GSAP innerText changes
        don't conflict with in-flight fade animations."""
        if not words:
            return []
        chunks: list[list[Word]] = []
        current: list[Word] = []
        for w in words:
            current.append(w)
            ends_phrase = bool(w.text and w.text[-1] in ",.!?")
            if ends_phrase or len(current) >= 3:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        result: list[dict] = []
        for i, ch in enumerate(chunks):
            start = ch[0].start_s
            natural_end = ch[-1].end_s + 0.15
            # Cap end at next chunk's start (minus 50ms gap) to prevent overlap
            if i + 1 < len(chunks):
                cap_end = chunks[i + 1][0].start_s - 0.05
                end = min(natural_end, cap_end)
            else:
                end = natural_end
            # Ensure end > start (defensive)
            end = max(start + 0.3, end)
            result.append({
                "text": " ".join(x.text.rstrip(",.!?:;") for x in ch).upper(),
                "start": round(start, 2),
                "end": round(end, 2),
            })
        return result

    def _compose_via_hyperframes(
        self, title: str, scenes: list[Scene], vo_path: Path,
        words: list[Word], mood: str,
    ) -> Path:
        """Build a HyperFrames composition directory and render to MP4."""
        from string import Template

        safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:80]
        work_dir = OUTPUT_DIR / "_hf"
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True)

        assets = work_dir / "assets"
        for sub in ("audio", "sfx", "music", "footage"):
            (assets / sub).mkdir(parents=True, exist_ok=True)

        # Voiceover
        shutil.copy(vo_path, assets / "audio" / "voiceover.mp3")

        # Music (mood-based)
        music_root = ASSETS_DIR / "music"
        mood_dir = music_root / (mood if mood in {"urgent", "upbeat", "calm", "neutral"} else "neutral")
        candidates = list(mood_dir.glob("*.mp3")) if mood_dir.exists() else list(music_root.glob("*.mp3"))
        has_music = False
        if candidates:
            import random
            shutil.copy(random.choice(candidates), assets / "music" / "bg.mp3")
            has_music = True

        # SFX
        sfx_root = ASSETS_DIR / "sfx"
        for sfx in ("whoosh.wav", "bass-hit.wav", "ding.wav"):
            src = sfx_root / sfx
            if src.exists():
                shutil.copy(src, assets / "sfx" / sfx)

        # Footage per scene
        scene_has_footage: list[bool] = []
        for i, scene in enumerate(scenes):
            target = assets / "footage" / f"scene_{i:02d}.mp4"
            if scene.footage_path and Path(scene.footage_path).exists():
                shutil.copy(scene.footage_path, target)
                scene_has_footage.append(True)
            else:
                scene_has_footage.append(False)

        # Build caption chunks
        chunks = self._build_caption_chunks_hf(words)
        total_dur = round((words[-1].end_s + 0.5), 2) if words else 30.0
        brand_out_at = round(max(0.0, total_dur - 0.6), 2)

        # Audio blocks
        audio_lines: list[str] = []
        audio_lines.append(
            '      <audio src="assets/audio/voiceover.mp3" data-start="0" data-volume="1.0" data-track-index="10"></audio>'
        )
        if has_music:
            audio_lines.append(
                f'      <audio src="assets/music/bg.mp3" data-start="0" data-duration="{total_dur}" data-volume="0.16" data-track-index="11"></audio>'
            )
        # 2-3 whoosh transitions max (restraint per SKILL) — only if file exists
        whoosh_present = (assets / "sfx" / "whoosh.wav").exists()
        if whoosh_present and len(scenes) > 1:
            mid = max(1, len(scenes) // 2)
            whoosh_pts = sorted({round(t, 2) for t in [scenes[1].start_s, scenes[mid].start_s, scenes[-1].start_s] if 0 < t < total_dur})[:3]
            for i, t in enumerate(whoosh_pts):
                audio_lines.append(
                    f'      <audio src="assets/sfx/whoosh.wav" data-start="{t}" data-volume="0.4" data-track-index="{20+i}"></audio>'
                )
        # Bass hit on hook (only if file exists)
        if (assets / "sfx" / "bass-hit.wav").exists() and total_dur > 1.0:
            audio_lines.append(
                f'      <audio src="assets/sfx/bass-hit.wav" data-start="0.0" data-volume="0.55" data-track-index="25"></audio>'
            )
        # Ding at CTA (only if file exists)
        if (assets / "sfx" / "ding.wav").exists() and total_dur > 4.0:
            ding_at = round(total_dur - 2.0, 2)
            audio_lines.append(
                f'      <audio src="assets/sfx/ding.wav" data-start="{ding_at}" data-volume="0.5" data-track-index="26"></audio>'
            )
        audio_blocks = "\n".join(audio_lines)

        # Scene blocks (footage if available, gradient fallback otherwise)
        grad_class = {
            "urgent": "grad-urgent",
            "upbeat": "grad-upbeat",
            "calm": "grad-calm",
            "neutral": "grad-neutral",
        }.get(mood, "grad-neutral")
        scene_html: list[str] = []
        for i, scene in enumerate(scenes):
            dur = round(max(0.5, scene.duration_s), 2)
            start = round(scene.start_s, 2)
            if scene_has_footage[i]:
                # data-start MUST be absolute composition time (matches scene start),
                # not 0 — otherwise all videos play simultaneously from t=0 and freeze
                # by the time their scene becomes visible.
                inner = (
                    f'<video src="assets/footage/scene_{i:02d}.mp4" '
                    f'muted playsinline preload="auto" '
                    f'data-start="{start}" data-duration="{dur}" data-media-start="0"></video>'
                )
            else:
                inner = f'<div class="grad-fallback {grad_class}"></div>'
            scene_html.append(
                f'      <div id="s{i}" class="scene" data-start="{start}" data-duration="{dur}" data-track-index="1">\n'
                f'        {inner}\n'
                f'        <div class="vignette"></div>\n'
                f'      </div>'
            )
        scene_blocks = "\n".join(scene_html)

        # Main timeline scene transitions: HARD CUTS (no fade through dark bg).
        # Reference reels use snap cuts on beat — keeps energy high, no black flash.
        # Subtle scale-in (kenburns) gives life without overlapping fades.
        tl_lines: list[str] = []
        for i, scene in enumerate(scenes):
            sid = f"#s{i}"
            start = round(scene.start_s, 2)
            dur = round(max(0.5, scene.duration_s), 2)
            end = round(start + dur, 2)
            # Hard cut IN at scene start
            tl_lines.append(
                f'      main.set("{sid}", {{ opacity: 1, scale: 1.04 }}, {start});'
            )
            # Subtle kenburns scale during scene (no fade)
            tl_lines.append(
                f'      main.to("{sid}", {{ scale: 1.0, duration: {dur}, ease: "power1.out" }}, {start});'
            )
            # Hard cut OUT at scene end (last scene fades for outro)
            if i < len(scenes) - 1:
                tl_lines.append(
                    f'      main.set("{sid}", {{ opacity: 0 }}, {end});'
                )
            else:
                fade_at = round(end - 0.3, 2)
                tl_lines.append(
                    f'      main.to("{sid}", {{ opacity: 0, duration: 0.3, ease: "power2.in" }}, {fade_at});'
                )
        main_timeline_lines = "\n".join(tl_lines)

        # Render template
        tpl_path = PLUGIN_DIR / "composition_template.html"
        template = Template(tpl_path.read_text(encoding="utf-8"))
        html = template.substitute(
            title=safe_title,
            total_dur=str(total_dur),
            brand_out_at=str(brand_out_at),
            audio_blocks=audio_blocks,
            scene_blocks=scene_blocks,
            phrases_json=json.dumps(chunks, ensure_ascii=False),
            main_timeline_lines=main_timeline_lines,
        )
        (work_dir / "index.html").write_text(html, encoding="utf-8")

        # Sidecar config files (matching `hyperframes init` scaffold)
        (work_dir / "hyperframes.json").write_text(json.dumps({
            "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
            "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
            "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
        }))
        (work_dir / "package.json").write_text(json.dumps({"name": "qanot-reel"}))
        (work_dir / "meta.json").write_text(json.dumps({
            "id": "qanot-reel", "name": "qanot-reel", "createdAt": "2026-05-06T00:00:00Z",
        }))

        # Run hyperframes render. --no-browser-gpu (no GPU in container),
        # --workers 2 (cap RAM usage), --quality standard.
        logger.info("[reels] hyperframes render starting (workdir=%s, scenes=%d, dur=%.1fs)",
                    work_dir, len(scenes), total_dur)
        result = subprocess.run(
            ["hyperframes", "render", "--no-browser-gpu", "--workers", "2", "--quality", "standard"],
            cwd=str(work_dir),
            capture_output=True,
            timeout=900,
        )
        if result.returncode != 0:
            err = (result.stderr or b"").decode(errors="replace")[:800]
            logger.error("[reels] hyperframes render failed: %s", err)
            raise RuntimeError(f"hyperframes render failed: {err}")

        # Find the rendered MP4
        renders_dir = work_dir / "renders"
        mp4_files = sorted(renders_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not mp4_files:
            raise RuntimeError("hyperframes produced no MP4")

        final = OUTPUT_DIR / f"{safe_title}.mp4"
        if final.exists():
            final.unlink()
        shutil.move(str(mp4_files[0]), str(final))
        logger.info("[reels] hyperframes render done → %s", final)
        return final
