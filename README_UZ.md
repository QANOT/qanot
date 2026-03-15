<p align="center">
  <h1 align="center">🪶 Qanot AI</h1>
  <p align="center">
    <strong>O'zi uchib ketadigan AI agent.</strong><br>
    <em>Ikki buyruq — va uchadi.</em>
  </p>
  <p align="center">
    <a href="https://pypi.org/project/qanot/"><img src="https://img.shields.io/pypi/v/qanot?color=blue&label=PyPI" alt="PyPI"></a>
    <a href="https://pypi.org/project/qanot/"><img src="https://img.shields.io/badge/python-3.11+-blue" alt="Python 3.11+"></a>
    <a href="https://github.com/QANOT/qanot/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
    <a href="https://github.com/QANOT/qanot/stargazers"><img src="https://img.shields.io/github/stars/QANOT/qanot?style=social" alt="Stars"></a>
  </p>
  <p align="center">
    <a href="https://plane.topkey.uz/docs/uz/">Docs</a> · <a href="https://plane.topkey.uz/docs/">English</a> · <a href="https://plane.topkey.uz">Sayt</a> · <a href="https://t.me/qanot_bot">Telegram</a>
  </p>
</p>

> **Qanot** — Telegram uchun AI agent yaratadigan yengil Python framework. Xotira, toollar, RAG, multi-agent delegatsiya, multi-provider failover — hammasi tayyor.

---

## Tez boshlash

```bash
pip install qanot
qanot init        # Provider, model tanlaysiz, config yaratiladi
qanot start       # Bot ishlaydi
```

Tamom. Agent uchib ketdi.

---

## Nimasi boshqacha

**Agent Loop** — Har bir savol uchun 25 tagacha tool ishlatadi. Circuit breaker, loop detection, xatolarni o'zi tuzatadi.

**3 bosqichli routing** — Oddiy savol → Haiku (arzon). Murakkab savol → Opus (kuchli). API xarajatini 50-60% tejaydi.

**Multi-Agent** — Agentlar bir-biri bilan gaplashadi. Vazifa topshirish, suhbat, background workerlar.

**5 ta provider** — Claude, GPT, Gemini, Groq, Ollama. Biri tushsa, ikkinchisiga avtomatik o'tadi.

**3 bosqichli xotira** — WAL real-time tuzatishlarni ushlaydi. Kunlik qaydlar. MEMORY.md — uzoq muddatli xotira. Agent o'z SOUL.md ni o'zi yaxshilaydi.

**RAG** — Vector + FTS5 hybrid qidiruv. FastEmbed CPU embedder — GPU kerak emas.

**Ovoz** — 4 ta provider (Muxlisa, KotibAI, Aisha, Whisper). O'zbek tilida STT + TTS.

**Streaming** — Telegram `sendMessageDraft` (Bot API 9.5) — haqiqiy real-time streaming.

**115+ plugin tool** — amoCRM (34), Bitrix24 (30), 1C Enterprise (18), AbsMarket (32). O'zingiz ham yozishingiz mumkin.

**Xavfsizlik** — 3 bosqichli exec security, rate limiting, SSRF himoya, fayl tekshiruv, SecretRef.

---

## Arxitektura

```
User → Telegram → Agent Loop (25 iteratsiya max)
                      ├── Model Router (Haiku / Sonnet / Opus)
                      ├── LLM Provider (Claude / GPT / Gemini / Groq / Ollama)
                      ├── Tool Registry (35+ tayyor + 115 plugin tool)
                      ├── Xotira (WAL → kunlik qayd → uzoq muddatli)
                      ├── RAG Engine (FastEmbed + FTS5 hybrid)
                      ├── Ovoz (4 provider)
                      ├── Multi-Agent (delegate / converse / spawn)
                      └── Xavfsizlik (rate limit + exec approval + file jail)
```

---

## CLI

```bash
qanot init                  # Sozlash wizard
qanot start / stop / restart
qanot status / logs
qanot update                # PyPI dan yangilash
qanot doctor --fix          # Diagnostika + avtomatik tuzatish
qanot config show / set
qanot plugin new <name>     # Yangi plugin yaratish
```

---

## Pluginlar

Tayyor integratsiyalar:

| Plugin | Toollar | Nima qiladi |
|--------|---------|------------|
| **amoCRM** | 34 | Lidlar, kontaktlar, kompaniyalar, vazifalar, chatlar, teglar |
| **Bitrix24** | 30 | Sdelkalar, lidlar, kontaktlar, schyotlar, tovarlar, vazifalar |
| **1C Enterprise** | 18 | Kontragentlar, tovarlar, sotuvlar, xaridlar, kassa, qoldiqlar |
| **AbsMarket** | 32 | POS sotuvlar, xaridlar, ombor, mijozlar, ta'minotchilar + SQL |

O'zingiz ham yozing:

```python
from qanot.plugins.base import Plugin, tool

class QanotPlugin(Plugin):
    name = "mening_plugin"

    @tool("mening_tool", "Bu tool nima qiladi")
    async def mening_tool(self, params: dict) -> str:
        return '{"natija": "tayyor"}'
```

---

## Docs

| | English | O'zbekcha |
|---|---|---|
| Boshlash | [docs](https://plane.topkey.uz/docs/getting-started/) | [docs](https://plane.topkey.uz/docs/uz/getting-started/) |
| To'liq qo'llanma | [docs](https://plane.topkey.uz/docs/GUIDE/) | [docs](https://plane.topkey.uz/docs/uz/GUIDE/) |
| Sozlash | [docs](https://plane.topkey.uz/docs/configuration/) | [docs](https://plane.topkey.uz/docs/uz/configuration/) |
| Toollar | [docs](https://plane.topkey.uz/docs/tools/) | [docs](https://plane.topkey.uz/docs/uz/tools/) |
| Pluginlar | [docs](https://plane.topkey.uz/docs/plugins/) | [docs](https://plane.topkey.uz/docs/uz/plugins/) |
| Arxitektura | [docs](https://plane.topkey.uz/docs/architecture/) | [docs](https://plane.topkey.uz/docs/uz/architecture/) |
| API Reference | [docs](https://plane.topkey.uz/docs/api-reference/) | [docs](https://plane.topkey.uz/docs/uz/api-reference/) |

---

## Hissa qo'shish

```bash
git clone https://github.com/QANOT/qanot.git
cd qanot
pip install -e .
python -m pytest tests/ -v   # 757 test
```

---

## Litsenziya

MIT — ishlatavering, fork qilavering, build qilavering.

---

<p align="center">
  <strong>Toshkentda yaratilgan 🇺🇿</strong><br>
  <sub>Qanot — agentlaringizga qanat beradi.</sub>
</p>
