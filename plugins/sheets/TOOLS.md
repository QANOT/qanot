# Google Sheets Tools

10 ta tool — Google Sheets v4 + Drive v3 ustida, `drive.file` OAuth ruxsati bilan.

## Connection

### `sheets_health`
Ulanish holatini tekshiradi: OAuth token ishlayaptimi, qaysi jadvallar ulangan.

```json
{}
```

### `sheets_list_connected`
Bot-ga ulangan jadvallar ro'yxati.

```json
{}
```

## Create & Manage

### `sheets_create`
Yangi bo'sh jadval yaratadi. `drive.file` scope sabab, yaratilgan jadval avtomatik ulanadi (qayta Picker kerak emas).

```json
{
  "title": "Savdolar 2026-04-21",
  "tab_name": "Sotuvlar",
  "headers": ["Sana", "Mijoz", "Summa (so'm)", "To'lov turi"]
}
```

### `sheets_list_tabs`
Jadval ichidagi tab (sheet) nomlari.

```json
{"spreadsheet_id": "1abc..."}
```

## Read

### `sheets_read`
A1 diapazon bo'yicha o'qiydi.

```json
{"spreadsheet_id": "1abc...", "range": "Sotuvlar!A1:D100"}
```

### `sheets_search`
Tab ichida qator qidiradi (har qaysi hujayrada substring izlaydi).

```json
{"tab": "Mijozlar", "query": "Akmal", "limit": 10}
```

## Write

### `sheets_append`
Tab oxiriga yangi qator(lar) qo'shadi.

```json
{
  "range": "Sotuvlar",
  "values": [["2026-04-21", "Akmal", 150000, "naqd"]]
}
```

### `sheets_update`
Aniq bir diapazonni qayta yozadi (mavjud qiymatlar o'chadi).

```json
{
  "range": "Sotuvlar!C5",
  "values": [[175000]]
}
```

## Share & Disconnect

### `sheets_share`
Jadvalni email orqali ulashadi (Google e-mail yuboradi).

```json
{"email": "hisobchi@example.com", "role": "writer"}
```

### `sheets_disconnect`
Jadvalni sessiya xotirasidan olib tashlaydi. To'liq o'chirish uchun Google Account → Security → Third-party apps orqali Qanot-ni chiqarib tashlash kerak.

```json
{"spreadsheet_id": "1abc..."}
```

## Notes

- `spreadsheet_id` ixtiyoriy — berilmasa default sheet ishlatiladi
- `spreadsheet_id` o'rniga sheet nomini ham yozish mumkin (case-insensitive)
- `drive.file` scope sabab, faqat foydalanuvchi Picker-da tanlagan yoki agent o'zi yaratgan sheet-larga ruxsat bor
- `sheets_search` katta jadvallar (>10k qator) uchun sekin — server-side text search mavjud emas
