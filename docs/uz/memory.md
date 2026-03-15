# Xotira tizimi

Qanot AI uch darajali xotira tizimiga ega: sessiya holati (qisqa muddatli), kunlik yozuvlar (o'rta muddatli) va MEMORY.md (uzoq muddatli). WAL protokoli ularni bog'laydi -- har bir foydalanuvchi xabarini agent javob berishdan oldin skanerlaydi.

## WAL protokoli (Write-Ahead Logging)

Asosiy g'oya: agent javob yaratishdan oldin har bir foydalanuvchi xabari muhim ma'lumotlar uchun skaner qilinadi va `SESSION-STATE.md` ga yoziladi. Bu tuzatishlar, afzalliklar va qarorlar -- suhbat keyinchalik qisqartirilsa ham -- saqlanishini ta'minlaydi.

### Qanday ishlaydi

1. Foydalanuvchi xabar yuboradi
2. `wal_scan()` xabarga regex patternlarni qo'llaydi
3. Mos yozuvlar vaqt belgisi bilan `SESSION-STATE.md` ga qo'shiladi
4. Faqat shundan keyin agent xabarni qayta ishlab javob beradi

### Nima saqlanadi

| Kategoriya | Trigger patterni | Misol |
|-----------|-----------------|-------|
| `correction` | "actually", "no I meant", "it's not X, it's Y" | "Actually, my name is Sardor, not Sarvar" |
| `proper_noun` | "my name is", "I'm", "call me" + bosh harf bilan so'z | "My name is Bobur" |
| `preference` | "I like", "I prefer", "I don't like", "I want" | "I prefer dark mode" |
| `decision` | "let's do", "go with", "use" | "Let's go with PostgreSQL" |
| `specific_value` | Sanalar, URLlar, katta raqamlar | "The deadline is 2025-06-15" |
| `remember` | "remember this", "don't forget", "eslab qol", "unutma", "yodda tut" | "Remember that the API key rotates monthly" |

### Doimiy kategoriyalar

`proper_noun`, `preference` va `remember` kategoriyalaridagi yozuvlar `SESSION-STATE.md` ga qo'shimcha ravishda avtomatik `MEMORY.md` ga ham saqlanadi. Bular joriy sessiyadan keyingi davrlarda ham saqlanishi kerak bo'lgan faktlar hisoblanadi. Takrorlanishni aniqlash bir xil faktni ikki marta yozilishini oldini oladi.

### SESSION-STATE.md formati

```markdown
# SESSION-STATE.md -- Active Working Memory

- [2025-01-15T10:30:00+00:00] **proper_noun**: My name is Sardor
- [2025-01-15T10:31:00+00:00] **preference**: I prefer Python over JavaScript
- [2025-01-15T10:35:00+00:00] **decision**: let's use FastAPI for the backend
```

Bu fayl system promptga kiritiladi, shuning uchun agent doimo oxirgi sessiya kontekstiga kirish imkoniga ega.

## Kunlik yozuvlar

Har bir suhbat almashinuvi xulosalanib, `workspace/memory/YYYY-MM-DD.md` dagi kunlik yozuv fayliga qo'shiladi.

```markdown
# Daily Notes -- 2025-01-15

## [10:30:00]
**User:** Tell me about FastAPI...
**Agent:** FastAPI is a modern Python web framework...

## [10:35:00]
**User:** How do I set up authentication?...
**Agent:** For JWT authentication with FastAPI...
```

Kunlik yozuvlar o'rta muddatli xotira vazifasini bajaradi. `memory_search` tooli oxirgi 30 kunlik yozuv bo'ylab qidiradi. RAG yoqilgan bo'lsa, kunlik yozuvlar semantik qidiruv uchun ham indekslanadi.

## MEMORY.md (uzoq muddatli xotira)

`workspace/MEMORY.md` -- uzoq muddatli xotira fayli. Agent muhim faktlar, foydalanuvchi afzalliklari va loyiha kontekstini shu yerga yozadi. Kunlik yozuvlardan farqli o'laroq (sanaga bog'langan), MEMORY.md doimiy va agent tomonidan boshqariladi.

Agent SOUL.md ko'rsatmalariga asoslanib MEMORY.md ga nima yozishni hal qiladi. Odatiy yozuvlar:

- Foydalanuvchi afzalliklari va muloqot uslubi
- Loyiha konteksti va arxitektura qarorlari
- Takrorlanuvchi patternlar va o'rganilgan xulq-atvorlar

## Xotira qidiruvi

`memory_search` tooli uchala xotira darajasi bo'ylab qidiradi:

1. **MEMORY.md** -- uzoq muddatli faktlar
2. **Kunlik yozuvlar** -- oxirgi 30 kunlik suhbat xulosalari
3. **SESSION-STATE.md** -- joriy sessiya WAL yozuvlari

Qidiruv katta-kichik harfga bog'liq bo'lmagan substring moslik bo'yicha. RAG yoqilgan bo'lsa, qidiruv BM25 gibrid reytingli semantik vector qidiruvga yangilanadi (qarang: [RAG](rag.md)).

```python
# Agent memory_search ni query bilan chaqiradi
results = memory_search("FastAPI authentication", workspace_dir)
# Qaytaradi: [{"file": "memory/2025-01-15.md", "line": 12, "content": "..."}]
```

## Kontekst boshqaruvi va siqish

Suhbatlar o'sgan sari kontekst oynasi to'ladi. Qanot token sarfini kuzatadi va ma'lum chegaralarda choralar ko'radi.

### Ishchi bufer (50% chegara)

Kontekst sarfi 50% ga yetganda, ishchi bufer faollashadi:

- Xotira papkasida `working-buffer.md` fayli yaratiladi
- Har bir almashish (foydalanuvchi xabari + agent xulosasi) bu faylga qo'shiladi
- Siqish muhim kontekstni yo'qotgan holatda zaxira vazifasini bajaradi

```markdown
# Working Buffer (Danger Zone Log)
**Status:** ACTIVE
**Started:** 2025-01-15T14:30:00+00:00

---

## [2025-01-15 14:30:00] Human
Can you refactor the database module?

## [2025-01-15 14:30:00] Agent (summary)
Refactored the database module to use connection pooling...
```

### Proaktiv siqish (60% chegara)

Taxminiy keyingi-navbat konteksti maksimalning 60% dan oshganda:

1. Dastlabki 2 xabar (boshlang'ich kontekst) saqlanadi
2. Oxirgi 4 xabar (yaqin kontekst) saqlanadi
3. Oraliqdagi hamma narsa olib tashlanadi
4. Nima bo'lganini tushuntiruvchi xulosa markeri qo'yiladi

```
[CONTEXT COMPACTION: 12 earlier messages were removed to free context space.
Recent conversation preserved below. Check your workspace files
(SESSION-STATE.md, memory/) for any important context from earlier.]
```

Siqishdan keyin token taxmini maksimalning taxminan 35% ga tuzatiladi.

### Siqishdan tiklash

Agent siqish belgilarini aniqlasa (qisqartirish markerlari, "qayerda to'xtagan edik?" xabarlari), quyidagilardan tiklash kontekstini avtomatik kiritadi:

1. Ishchi bufer mazmuni
2. SESSION-STATE.md yozuvlari
3. Bugungi kunlik yozuvlar

Bu tiklash foydalanuvchi xabariga qo'shiladi, shunda agent muhim kontekstni yo'qotmasdan qayta yo'nalishi mumkin.

### Tool natijalarini qisqartirish

8000 belgidan oshgan tool natijalari kontekst shishishini oldini olish uchun qisqartiriladi. Qisqartirish boshdan 70% va oxirdan 20% ni saqlaydi, o'rtada necha belgi olib tashlangani ko'rsatilgan marker qo'yiladi.

## Xotira yozish hooklari

Xotira yozilganda (WAL yozuvlari, kunlik yozuvlar), ro'yxatdan o'tgan hooklar xabardor qilinadi. RAG tizimi budan xotira mazmunini avtomatik qayta indekslash uchun foydalanadi:

```python
# Ichki hook ro'yxatdan o'tkazish (main.py da avtomatik bajariladi)
def on_memory_write(content: str, source: str) -> None:
    asyncio.create_task(rag_indexer.index_text(content, source=source))

add_write_hook(on_memory_write)
```

Bu RAG qidiruv natijalari qo'lda qayta indekslashsiz eng so'nggi xotira yozuvlarini o'z ichiga olishini anglatadi.

## Fayl joylashuvi

| Fayl | Maqsad | System promptga kiritilgan |
|------|--------|---------------------------|
| `workspace/SESSION-STATE.md` | Joriy sessiya WAL yozuvlari | Ha |
| `workspace/MEMORY.md` | Uzoq muddatli xotira | Ha ("Your Long-Term Memory" bo'limi sifatida) |
| `workspace/memory/YYYY-MM-DD.md` | Kunlik suhbat yozuvlari | Yo'q (talab bo'yicha qidiriladi) |
| `workspace/memory/working-buffer.md` | Xavfli zona zaxira logi | Faqat siqishdan tiklash vaqtida |
