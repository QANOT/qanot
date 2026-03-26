# RAG (Retrieval-Augmented Generation)

Qanot AI da hujjatlarni indekslash va suhbatlar davomida tegishli ma'lumotlarni olish uchun o'rnatilgan RAG tizimi bor. Vector o'xshashlik va BM25 kalit so'z mosligi birlashtirilgan gibrid qidiruv ishlatiladi.

## Umumiy ko'rinish

RAG agentga kontekst oynasiga sig'maydigan hujjatlardan qidirish imkonini beradi. To'liq fayllarni system promptga kiritish o'rniga, hujjatlar bo'laklarga bo'linadi, embedding qilinadi va lokal SQLite bazasida saqlanadi. Agent ma'lumot kerak bo'lganda semantik qidiradi.

**RAG qachon kerak:**
- Bot katta hujjatlarga murojaat qilishi kerak (bilim bazalari, qo'llanmalar, loglar)
- Agentning o'tgan suhbatlarni eslab qolishi va qidirishi kerak
- Faqat kalit so'z emas, semantik (ma'no asosida) qidiruv kerak

**RAG kerak bo'lmaydigan holatlar:**
- Bot faqat qisqa suhbat yuritadi, hujjatlarga murojaat yo'q
- Barcha kerakli kontekst system promptga sig'adi (SOUL.md, TOOLS.md)
- Embedding qo'llab-quvvatlamaydigan provider ishlatyapsiz va boshqasini qo'sha olmaysiz

## O'rnatish

### Talablar

1. RAG dependencyni o'rnating:

```bash
pip install qanot[rag]
```

Bu vector operatsiyalar uchun SQLite kengaytmasi `sqlite-vec` ni o'rnatadi.

2. Gemini yoki OpenAI provider sozlangan bo'lishi kerak. Anthropic va Groq embedding API taqdim etmaydi.

3. Configda RAG ni yoqing (standart holda yoqilgan):

```json
{
  "rag_enabled": true
}
```

### Embedding provider avtomatik tanlash

Qanot mavjud configingizdan eng yaxshi embedding providerni avtomatik tanlaydi. Qo'shimcha API kalit yoki sozlash shart emas.

**Ustunlik tartibi:**

| Ustunlik | Provider | Model | O'lchamlar | Narx |
|----------|----------|-------|-----------|------|
| 0 | FastEmbed | `nomic-ai/nomic-embed-text-v1.5` | 768 | Bepul (CPU, ONNX) |
| 1 | Gemini | `gemini-embedding-001` | 3072 | Bepul daraja |
| 2 | OpenAI | `text-embedding-3-small` | 1536 | $0.02/MTok |

Ollama provider aniqlanganda FastEmbed avtomatik tanlanadi. ONNX runtime orqali CPU da ishlaydi, chat modeli bilan GPU VRAM to'qnashuvlarini oldini oladi. `pip install fastembed` bilan o'rnating.

Embedder ko'p providerli va bitta providerli configlarni tekshiradi. Failover uchun Gemini provideringiz bo'lsa, uning API kaliti embeddinglar uchun qayta ishlatiladi.

**Misol:** Configingizda Anthropic asosiy provider va Gemini failover sifatida bo'lsa, RAG embeddinglar uchun Gemini ni ishlatadi:

```json
{
  "providers": [
    {"name": "main", "provider": "anthropic", "model": "claude-sonnet-4-6", "api_key": "sk-ant-..."},
    {"name": "backup", "provider": "gemini", "model": "gemini-2.5-flash", "api_key": "AIza..."}
  ]
}
```

Natija: Chat -- Anthropic, embeddinglar -- Gemini (bepul).

Mos embedding provider topilmasa, RAG log ogohlantirishi bilan jimgina o'chiriladi.

## Qanday ishlaydi

### Indekslash jarayoni

```
Hujjat matni
    |
    v
chunk_text() -- ~512-tokenli bo'laklarga bo'lish, 64-token overlap
    |
    v
Embedder.embed() -- Bo'laklarni vectorlarga aylantirish (100 tadan batch)
    |
    v
SqliteVecStore.add() -- Vectorlar + metadata ni SQLite da saqlash
    |
    v
BM25Index.add() -- Kalit so'z qidiruvi uchun bo'lak matnini indekslash
```

**Bo'laklash strategiyasi:**
1. Ikki qo'sh yangi qator (paragraflar) bo'yicha bo'lish
2. Paragraf bo'lak hajmidan oshsa, gaplar bo'yicha bo'lish
3. Gap hali ham oshsa, so'zlar bo'yicha bo'lish
4. Kichik segmentlarni bo'laklar orasida overlap bilan birlashtirish

Token taxmini: 1 token = 4 belgi.

### Qidiruv jarayoni

```
So'rov matni
    |
    +-- Embedder.embed_single() --> Vector o'xshashlik qidiruvi (sqlite-vec)
    |                                    |
    +-- BM25Index.search() -----------> Kalit so'z mosligi
    |                                    |
    v                                    v
         Reciprocal Rank Fusion (RRF)
              |
              v
         Tartiblangan natijalar (top_k)
```

**Gibrid qidiruv** ikkala signalni vaznli reciprocal rank fusion bilan birlashtiradi:
- Vector vazni: 70% (semantik ma'no)
- BM25 vazni: 30% (aniq kalit so'z mosligi)
- RRF konstantasi k=60

Bu gibrid yondashuv semantik qidiruv aniq atamalarni o'tkazib yuborgan va kalit so'z qidiruvi perifraz qilingan tushunchalarni o'tkazib yuborgan holatlarni hal qiladi.

### Saqlash

RAG `sqlite-vec` kengaytmali SQLite ishlatadi. Baza `workspace_dir/rag.db` da saqlanadi.

Sxema:
- `chunks` jadvali: matn mazmuni, manba identifikatori, user_id, metadata JSON, vaqt belgisi
- `chunks_vec` virtual jadvali: o'xshashlik qidiruvi uchun float32 vectorlar
- Filtrlangan so'rovlar uchun `source` va `user_id` bo'yicha indekslar

## RAG toollar

Agent suhbat davomida to'rtta RAG toolga ega:

### rag_index

Faylni RAG tizimiga indekslash.

```
Tool: rag_index
Input: {"path": "report.md", "name": "Q4 Report"}
```

- `.txt`, `.md`, `.csv`, `.pdf` fayllarni qo'llab-quvvatlaydi
- Yo'l workspace ga nisbiy yoki absolyut
- Manbani qayta indekslash eski bo'laklarni avval o'chiradi
- Bo'laklar sonini qaytaradi

### rag_search

Indekslangan hujjatlardan qidirish.

```
Tool: rag_search
Input: {"query": "quarterly revenue figures", "top_k": 5}
```

- Matn, manba va ball bilan tartiblangan natijalarni qaytaradi
- Natijalar vector va kalit so'z mosliklarini o'z ichiga oladi
- Suhbatdan chaqirilganda user_id bo'yicha filtrlanadi

### rag_list

Barcha indekslangan hujjat manbalarini ko'rsatish.

```
Tool: rag_list
Input: {}
```

Manba nomlari, bo'laklar soni va indekslash vaqt belgilarini qaytaradi.

### rag_forget

Hujjatni indeksdan o'chirish.

```
Tool: rag_forget
Input: {"source": "Q4 Report"}
```

Berilgan manba uchun barcha bo'laklarni o'chiradi va BM25 indeksni tozalaydi.

## Xotira integratsiyasi

RAG agentning xotira fayllarini avtomatik indekslaydi:

1. **Ishga tushganda:** `index_workspace()` MEMORY.md, SESSION-STATE.md, oxirgi 30 kunlik yozuvlar va `memories/` papkasidagi barcha fayllarni indekslaydi
2. **Xotira yozilganda:** WAL yozuvlari yoki kunlik yozuvlar yozilganda write hook qayta indekslashni ishga tushiradi
3. **`/memories` papkasi:** Anthropic xotira tooli yaratgan fayllar ham avtomatik indekslanadi
4. **Content-hash deduplikatsiya:** Fayllar faqat mazmuni o'zgarganda qayta indekslanadi

O'rnatilgan `memory_search` tooli avval RAG ni tekshiradi (mavjud bo'lsa), keyin substring qidiruvga qaytadi:

```python
# memory_search tool xulq-atvori:
if rag_indexer is not None:
    results = await rag_indexer.search(query)  # Semantik qidiruv
    if results:
        return results
# Zaxira: fayllar bo'ylab substring qidiruv
results = memory_search(query, workspace_dir)
```

## RAG rejimlari

Qanot turli model imkoniyatlarini boshqarish uchun uchta RAG rejimini qo'llab-quvvatlaydi:

```json
{
  "rag_mode": "auto"
}
```

| Rejim | Xulq-atvor | Mos keladi |
|-------|-----------|------------|
| `"auto"` (standart) | Har bir xabarga top 3 xotira ko'rsatmalarini avtomatik kiritadi + chuqurroq so'rovlar uchun `rag_search` toolni saqlaydi | Barcha modellar -- kichik/arzon modellar bilan ham ishlaydi |
| `"agentic"` | Avtomatik kiritish yo'q. Agent o'zi qaror qilganda `rag_search` toolni ishlatadi | Aqlli modellar (Claude, GPT-4) -- toollarni ishonchli ishlatadi |
| `"always"` | `auto` bilan bir xil -- har doim kontekst ko'rsatmalarini kiritadi | Modeldan qat'iy nazar kafolatlangan kontekst kerak bo'lganda |

### Nima uchun bu muhim

Barcha modellar qidiruv kerakligini bir xil darajada hal qila olmaydi:

| Model | `rag_search` ni o'zi chaqiradimi? |
|-------|--------------------------------------|
| Claude Sonnet/Opus | Ha |
| GPT-4.1 | Ha |
| Gemini Pro | Ko'pincha |
| Llama 3.3 70B (Groq) | Ishonchsiz |
| Llama 3.1 8B (Groq) | Deyarli hech qachon |

`"auto"` rejimida har bir foydalanuvchi xabari (10 belgidan uzun) xotira bo'yicha yengil semantik qidiruvni ishga tushiradi. Top 3 natija `[MEMORY CONTEXT]` ko'rsatmalari sifatida qo'shiladi. Bu har bir xabar uchun bitta embedding API chaqiruviga tushadi, lekin kichik modellar ham tegishli kontekstga ega bo'lishini ta'minlaydi.

`"agentic"` rejimida hech narsa kiritilmaydi -- model o'zi `rag_search` ni tool sifatida chaqirishga qaror qilishi kerak. Embedding xarajatlarini tejash uchun faqat aqlli modellar bilan ishlating.

### Avtomatik kiritish qanday ishlaydi

```
User: "What was the API endpoint we discussed?"
                    |
_prepare_turn() -- WAL skanerlash
                    |
RAG qidiruv: top 3 xotira natijasi
                    |
Xabar shunga aylanadi:
  "What was the API endpoint we discussed?
   ---
   [MEMORY CONTEXT -- relevant past information]
   - [memory/2026-03-10.md] Discussed API endpoint /v2/users for...
   - [SESSION-STATE.md] Decision: use REST API with /v2 prefix..."
                    |
LLM kontekst bilan javob beradi (oddiy modellar ham to'g'ri ishlaydi)
```

`rag_search` tooli 3 ta avtomatik kiritilgan ko'rsatmalardan tashqari aniq chuqurroq qidiruvlar uchun hali ham mavjud.

## Sozlash

RAG xulq-atvori config va manba konstantalari bilan boshqariladi:

| Sozlash | Qiymat | Joylashuv |
|---------|--------|-----------|
| RAG rejimi | `"auto"` / `"agentic"` / `"always"` | `config.json: rag_mode` |
| Bo'lak hajmi | 512 token (~2048 belgi) | `RAGEngine.chunk_size` |
| Bo'lak overlap | 64 token (~256 belgi) | `RAGEngine.chunk_overlap` |
| BM25 vazni | 0.3 (30%) | `RAGEngine.bm25_weight` |
| Avtomatik kiritish soni | 3 natija | `agent.py: _prepare_turn()` |
| Min xabar uzunligi | 10 belgi ("hi", "ok" ni o'tkazib yuboradi) | `agent.py: _prepare_turn()` |
| Embedding batch hajmi | 100 matn har bir API chaqiruvda | `Embedder.embed()` |
| Qo'llab-quvvatlanadigan fayl turlari | `.txt`, `.md`, `.csv`, `.pdf` | `tools/rag.py` |
| Indekslanadigan kunlik yozuvlar | 30 ta eng so'nggi | `MemoryIndexer` |

## Cheklovlar

- **Embedding providerlar:** Faqat Gemini va OpenAI embeddinglarni qo'llab-quvvatlaydi. Anthropic va Groq qo'llab-quvvatlamaydi.
- **sqlite-vec talab qilinadi:** `pip install sqlite-vec` siz vector qidiruv o'chiriladi. Metadata jadvali ishlaydi, lekin o'xshashlik qidiruvi bo'sh natija qaytaradi.
- **Xotiradagi BM25:** BM25 indeks ishga tushganda va manba o'chirilgandan keyin noldan qayta quriladi. Diskka saqlanmaydi.
- **PDF uchun PyMuPDF kerak:** PDF parsing PyMuPDF ishlatadi (`pip install PyMuPDF`), `pip install qanot[rag]` ga kiritilgan. Usiz PDF indekslash o'rnatish ko'rsatmalari bilan xato qaytaradi.
- **Bitta foydalanuvchi doirasi:** Natijalar user_id bo'yicha filtrlanadi, lekin vector store barcha foydalanuvchilar orasida umumiy. Ko'p foydalanuvchili sozlashlarda barcha foydalanuvchilar hujjatlari bitta embedding fazosini baham ko'radi.
