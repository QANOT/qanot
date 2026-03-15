# Telegram integratsiyasi

Qanot AI Telegram bot aloqasi uchun [aiogram 3.x](https://docs.aiogram.dev/) ishlatadi. U uchta javob rejimi, ikkita transport rejimi, fayl yuklash va foydalanuvchi nazoratini qo'llab-quvvatlaydi.

## Javob rejimlari

Javob rejimini config'da o'rnating:

```json
{
  "response_mode": "stream"
}
```

### stream (standart)

Telegram Bot API 9.5 `sendMessageDraft` orqali real-time harfma-harf streaming qiladi.

**Qanday ishlaydi:**

1. Agent LLM dan tokenlarni stream qila boshlaydi
2. Yig'ilgan matn `stream_flush_interval` oralig'ida draft sifatida yuboriladi (standart 0.8s)
3. Tool bajarilayotganda drafting to'xtatiladi — race condition'larni oldini oladi
4. Tool natijalari kelgandan keyin drafting yangi matn bilan davom etadi
5. Oxirgi `sendMessage` to'liq formatlangan javobni yuboradi

**Afzalliklari:** Eng past seziluvchi kutish. Foydalanuvchilar matnni real-time ko'rishadi.
**Kamchiliklari:** `sendMessageDraft` ni qo'llab-quvvatlaydigan yangi Telegram versiyasi kerak.

**Race condition boshqaruvi:** Agent tool chaqirganda draft yangilanishi to'xtatiladi. Bu draft yangilanishi va tool natijasi bir vaqtda kelishi muammosini oldini oladi — bu rendering artefaktlariga olib kelishi mumkin edi. Oxirgi draft matni kuzatiladi — ortiqcha yangilanishlar yuborilmaydi.

### partial

`editMessageText` orqali yuborilgan xabarni vaqti-vaqti bilan yangilab turadi.

**Qanday ishlaydi:**

1. Birinchi matn qismi boshlang'ich xabar sifatida yuboriladi
2. Keyingi matn yig'ilib, xabar oraliq bilan tahrirlanadi
3. Oxirgi tahrir HTML formatlashni qo'llaydi
4. Agar javob Telegram xabar limitidan (4,000 belgi) oshsa, qo'shimcha qismlar alohida xabar sifatida yuboriladi

**Afzalliklari:** Barcha Telegram klientlarda ishlaydi. Eski Bot API versiyalariga mos.
**Kamchiliklari:** Foydalanuvchilar xabar tahrirlanishini ko'rishadi (yalt-yult qiladi), streaming'ga qaraganda kamroq silliq.

### blocked

To'liq javobni kutadi, keyin bitta xabar yuboradi.

**Qanday ishlaydi:**

1. Qayta ishlash davomida yozish indikatori ko'rsatiladi
2. Agent loop'i to'liq bajariladi
3. Tayyor javob formatlangan xabar sifatida yuboriladi

**Afzalliklari:** Eng oddiy rejim. Qisman yangilanishlar yoki draftlar yo'q.
**Kamchiliklari:** Foydalanuvchilar hech narsa ko'rmasdan kutishadi. Uzun javoblar sezilarli kechikish qiladi.

## Transport rejimlari

### Polling (standart)

```json
{
  "telegram_mode": "polling"
}
```

Long polling — bot Telegram serverlariga ulanadi va yangilanishlarni kutadi. Ommaviy URL shart emas. Development va oddiy deploy uchun eng mos.

Ishga tushganda kutilayotgan yangilanishlar tashlanadi (`drop_pending_updates=True`) — eskirgan xabarlarni qayta ishlashni oldini oladi.

### Webhook

```json
{
  "telegram_mode": "webhook",
  "webhook_url": "https://bot.example.com",
  "webhook_port": 8443
}
```

Telegram'dan yangilanishlarni qabul qiladigan aiohttp web server ishlatadi. Ommaviy HTTPS URL kerak.

Webhook endpoint'i `{webhook_url}/webhook`. Qanot:

1. Ishga tushganda Telegram'ga webhook URL'ni o'rnatadi
2. `0.0.0.0:{webhook_port}` da aiohttp serverni ishga tushiradi
3. Kiruvchi yangilanishlarni o'sha dispatcher orqali qayta ishlaydi
4. O'chirilganda webhook'ni o'chiradi

**Reverse proxy bilan standart sozlash:**

```
Internet --> nginx (443) --> Qanot (8443)
```

```nginx
location /webhook {
    proxy_pass http://localhost:8443;
}
```

## Xabar boshqaruvi

Adapter uch turdagi xabarlarni qayta ishlaydi:

### Matnli xabarlar

Oddiy matnli xabarlar to'g'ridan-to'g'ri agent'ga yuboriladi.

### Rasmlar

Rasmlar `[Photo received]` prefiksi bilan qayd qilinadi. Caption (agar bo'lsa) qo'shiladi. Rasm kontenti qayta ishlanmaydi (hozirgi versiyada vision qo'llab-quvvatlanmaydi).

### Hujjatlar (fayl yuklash)

Hujjatlar avtomatik workspace'ga yuklab olinadi:

1. Fayl `{workspace_dir}/uploads/{filename}` ga yuklab olinadi
2. Xabar `[Fayl yuklandi: uploads/{filename}]` prefiksi bilan belgilanadi
3. Agent keyin `read_file` tool orqali faylni o'qiy oladi

Agar yuklab olish muvaffaqiyatsiz bo'lsa, xabarda shu qayd qilinadi va agent suhbatni davom ettiradi.

## Xabar formatlash

Agent javoblari yuborishdan oldin Markdown'dan Telegram HTML'ga o'giriladi:

| Markdown | HTML natijasi |
|----------|---------------|
| `**bold**` | `<b>bold</b>` |
| `` `code` `` | `<code>code</code>` |
| ```` ```code block``` ```` | `<pre>code block</pre>` |
| `# Heading` | `<b>Heading</b>` |
| Jadvallar (`\|...\|`) | `<pre>table</pre>` |
| `---` | Gorizontal chiziq (Unicode) |

HTML maxsus belgilari (`&`, `<`, `>`) konvertatsiyadan oldin escape qilinadi — injection'ni oldini oladi.

Agar HTML parsing xabar yuborishda muvaffaqiyatsiz bo'lsa, adapter oddiy matnga qaytadi.

### Xabar bo'lish

Telegram'da bitta xabar uchun 4,096 belgi limiti bor (Qanot 4,000 belgilik ish limitini ishlatadi). Uzun javoblar satr chegaralarida bo'linadi va oralarda 100ms kechikish bilan bir nechta xabar sifatida yuboriladi.

## Tool call tozalash

Ba'zi LLM provayderlar (ayniqsa Groq orqali Llama modellari) ba'zan tool call sintaksisini strukturalangan tool call o'rniga matn sifatida chiqaradi. Adapter bu artefaktlarni olib tashlaydi:

- `<function>...</function>` teglar
- `<tool_call>...</tool_call>` teglar
- Xom JSON tool call obyektlari

Bu foydalanuvchilarning bot javoblarida ichki tool call sintaksisini ko'rishini oldini oladi.

## Foydalanuvchi nazorati

```json
{
  "allowed_users": [123456789, 987654321]
}
```

`allowed_users` o'rnatilganda, faqat shu Telegram user ID'lar bot bilan muloqot qila oladi. Boshqa foydalanuvchilarning xabarlari jimgina e'tiborsiz qoldiriladi.

`allowed_users` bo'sh bo'lganda (standart), barcha foydalanuvchilar bot bilan muloqot qila oladi.

Telegram user ID'ingizni bilish uchun [@userinfobot](https://t.me/userinfobot) ga xabar yuboring.

## Bir vaqtdalik (Concurrency)

```json
{
  "max_concurrent": 4
}
```

Adapter bir vaqtda qayta ishlanadigan xabarlar sonini cheklash uchun asyncio semaphore ishlatadi. Agar 4 ta xabar bir vaqtda qayta ishlanayotgan bo'lsa, qo'shimcha xabarlar slot ochilguncha kutadi. Bu LLM provider'ni haddan tashqari ko'p parallel so'rovlar bilan bosib ketishni oldini oladi.

## Proaktiv xabarlar

Telegram adapter'i scheduler'ning xabar navbatini tekshiradigan proaktiv xabar loop'ini ishga tushiradi. Cron job natija berganda:

- **`proactive` xabarlar:** Barcha ruxsat etilgan foydalanuvchilarga yuboriladi
- **`system_event` xabarlar:** Asosiy agent'ning suhbatiga inject qilinadi

Batafsil: [Scheduler](scheduler.md)

## Xato boshqaruvi

Adapter agent xatolarini ushlaydi va foydalanuvchiga do'stona xabar yuboradi:

- **Rate limit xatolari:** "Limitga yetdik. Iltimos, 20-30 soniya kutib qayta yozing."
- **Boshqa xatolar:** "Xatolik yuz berdi. Iltimos, qayta urinib ko'ring."

Xatolar debugging uchun to'liq stack trace bilan loglanadi, lekin foydalanuvchilar faqat do'stona xabarni ko'rishadi.

## Guruh chat

```json
{
  "group_mode": "mention"
}
```

`group_mode` sozlamasi botning guruh va superguruh chatlardagi xatti-harakatini boshqaradi. Standart qiymat `"mention"`.

| Rejim | Xatti-harakat |
|-------|---------------|
| `off` | Bot barcha guruh xabarlarini e'tiborsiz qoldiradi |
| `mention` | Bot faqat username bilan @mention qilinganda yoki bot'ning o'z xabariga javob berilganda javob beradi |
| `all` | Bot guruhdagi har bir xabarga javob beradi |

**Mention rejimi qanday ishlaydi:**

1. Bot ishga tushganda o'z username'ini keshga oladi
2. Guruh xabari kelganda, xabar matnida yoki caption'da `@bot_username` bor-yo'qligini tekshiradi
3. Shuningdek xabar botning o'z xabariga javob ekanligini tekshiradi
4. Agar ikkala shart ham bajarilmasa, xabar jimgina e'tiborsiz qoldiriladi

**Guruh suhbat izolatsiyasi:** Guruh chatlarda barcha a'zolar `group_{chat_id}` bilan kalitlangan bitta suhbatni baham ko'rishadi. Ya'ni bot har bir guruh uchun bitta suhbat kontekstini saqlaydi, har bir foydalanuvchi uchun emas. DM'larda suhbatlar odatdagidek user ID bilan kalitlanadi.

**Yuboruvchi identifikatsiyasi:** Guruh xabarlari yuboruvchi ismi bilan prefikslangan (masalan, `[Ahmad]: xabar matni`) — agent guruh a'zolarini ajrata olishi uchun. `@bot_username` mention'i qayta ishlashdan oldin matndan olib tashlanadi.

## Ovozli xabarlar

Ovozli xabarlar va video yozuvlar voice API kaliti sozlanganda avtomatik transkripsiya qilinadi.

**Qayta ishlash oqimi:**

1. Ovozli xabar yoki video yozuv keladi
2. Bot darhol yozish indikatorini yuboradi
3. Audio vaqtinchalik faylga yuklab olinadi
4. Audio sozlangan `voice_provider` yordamida transkripsiya qilinadi
5. Transkripsiya qilingan matn ovozli xabar kontentini almashtiradi
6. Navbat odatdagidek qayta ishlanadi, `voice_request` bayrog'i o'rnatiladi

**Audio format boshqaruvi:**

| Provider | OGG qabul qiladi | Konvertatsiya kerak |
|----------|-------------------|---------------------|
| Muxlisa | Ha (native) | Yo'q |
| Whisper | Ha | Yo'q |
| KotibAI | Yo'q | OGG dan MP3 ga ffmpeg orqali |
| Aisha | Yo'q | OGG dan MP3 ga ffmpeg orqali |

Video yozuvlar uchun audio ffmpeg orqali chiqariladi (Muxlisa uchun OGG, boshqalar uchun MP3).

**TTS ovozli javoblar:**

`voice_mode` `"always"` bo'lganda yoki `"inbound"` bo'lib foydalanuvchi ovozli xabar yuborganda, bot matnli javobdan keyin TTS ovozli javob yuboradi. Oqim:

1. "Ovoz yozayapti" indikatori ko'rsatiladi
2. Oxirgi assistant javob matni TTS provider'ga yuboriladi
3. Qaytarilgan audio (WAV yoki URL) Telegram uchun OGG Opus'ga konvertatsiya qilinadi
4. Ovozli xabar `bot.send_voice()` orqali yuboriladi

**To'rtta voice provider:**

| Provider | STT | TTS | Ovozlar | Eslatmalar |
|----------|-----|-----|---------|------------|
| Muxlisa (standart) | Ha | Ha | maftuna, asomiddin | Native OGG, STT uchun ffmpeg shart emas |
| KotibAI | Ha | Ha | aziza, nargiza, soliha, sherzod, rachel, arnold | 6 ta ovoz, ko'p tilli |
| Aisha | Ha | Ha | gulnoza, jaxongir | Kayfiyat boshqaruvi (happy/sad/neutral) |
| Whisper | Ha | Yo'q | Yo'q | OpenAI, 50+ til, faqat STT |

**Iqtibos qilingan ovozli xabarlar:** Foydalanuvchi ovozli xabarga javob berganida, iqtibos qilingan ovoz ham transkripsiya qilinadi va javob izohida `[voice: transkripsiya qilingan matn]` sifatida qo'shiladi.

## Reaktsiyalar

```json
{
  "reactions_enabled": false
}
```

`reactions_enabled` `true` bo'lganda, bot qayta ishlash holatini ko'rsatish uchun xabarlarga emoji reaktsiyalar yuboradi:

| Emoji | Qachon |
|-------|--------|
| `eyes` | Xabar qabul qilindi, qayta ishlash boshlandi |
| `white_check_mark` | Qayta ishlash muvaffaqiyatli yakunlandi |
| `x` | Qayta ishlash davomida xato yuz berdi |

Xabarlar birlashtirilganda (bir nechta tez xabarlar bitta navbatga jamlanganda), oldingi xabarlarga `white_check_mark` reaktsiyasi qo'yiladi — ular qo'shilganini ko'rsatish uchun.

Reaktsiyalar `SetMessageReaction` API metodi orqali yuboriladi. Agar chatda reaktsiyalar qo'llab-quvvatlanmasa (masalan, eski guruhlar), xatolar jimgina e'tiborsiz qoldiriladi.

Standart qiymati `false` (reaktsiyalar yuborilmaydi).

## Javob rejimi (Reply Mode)

```json
{
  "reply_mode": "coalesced"
}
```

Bot'ning Telegram reply-to funksiyasini qachon ishlatishini boshqaradi.

| Rejim | Xatti-harakat |
|-------|---------------|
| `off` | Hech qachon reply-to qilmaydi; javoblar mustaqil xabar sifatida yuboriladi |
| `coalesced` | Faqat bir nechta tez xabarlar bitta navbatga birlashtirilganda reply-to qiladi |
| `always` | Har doim trigger xabarga reply-to qiladi |

Standart qiymati `"coalesced"`.

## Yozish indikatori

Qayta ishlash davomida bot har 4 soniyada yozish indikatorini yuboradi — javob tayyor bo'lguncha. Telegram klientda "Bot yozyapti..." sifatida ko'rinadi. Birinchi streaming draft yuborilishi bilanoq yozish loop'i bekor qilinadi.
