# Plugin tizimi

Qanot AI pluginlar orqali yangi toollar qo'shish, agentning shaxsini kengaytirish va tashqi servislar bilan integratsiya qilishni qo'llab-quvvatlaydi.

## Plugin arxitekturasi

Plugin -- bu kamida `plugin.py` fayli bo'lgan papka, ichida `Plugin` klassidan voris olingan klass bo'lishi kerak:

```
plugins/
└── myplugin/
    ├── plugin.py      # Majburiy: Plugin subclass
    ├── TOOLS.md       # Ixtiyoriy: workspace TOOLS.md ga qo'shiladigan hujjat
    └── helpers.py     # Ixtiyoriy: qo'shimcha modullar
```

Pluginlar ikkita joydan yuklanadi (navbat bilan tekshiriladi):

1. **O'rnatilgan:** paket ildizidagi `plugins/` papkasi
2. **Tashqi:** configdagi `plugins_dir` yo'li (standart: `/data/plugins`)

## Plugin yaratish

### 1-qadam: Plugin papkasini yarating

```bash
mkdir -p plugins/weather
```

### 2-qadam: plugin.py yozing

```python
from qanot.plugins.base import Plugin, ToolDef, tool

class QanotPlugin(Plugin):
    """Weather lookup plugin."""

    name = "weather"
    description = "Weather information for Uzbekistan cities"

    # Ixtiyoriy: workspace TOOLS.md ga qo'shiladigan matn
    tools_md = """
## Weather Tools

### weather_get
Get current weather for a city in Uzbekistan.
- **city**: City name (e.g., "Tashkent", "Samarkand")
"""

    # Ixtiyoriy: workspace SOUL.md ga qo'shiladigan matn
    soul_append = """
## Weather Behavior
When asked about weather, always use the weather_get tool.
Include temperature in both Celsius and Fahrenheit.
"""

    async def setup(self, config: dict) -> None:
        """Plugin yuklanayotganda chaqiriladi. Resurslarni shu yerda tayyorlang."""
        self.api_key = config.get("api_key", "")
        self.base_url = config.get("base_url", "https://api.weather.example.com")

    async def teardown(self) -> None:
        """Yopilayotganda chaqiriladi. Resurslarni tozalash shu yerda."""
        pass

    def get_tools(self) -> list[ToolDef]:
        """Tool definitionlarni qaytaradi. Dekoratorli metodlar uchun _collect_tools() ishlating."""
        return self._collect_tools()

    @tool(
        name="weather_get",
        description="Hozirgi ob-havo ma'lumotlari.",
        parameters={
            "type": "object",
            "required": ["city"],
            "properties": {
                "city": {
                    "type": "string",
                    "description": "Shahar nomi (masalan: Tashkent)",
                },
            },
        },
    )
    async def weather_get(self, params: dict) -> str:
        import aiohttp
        import json

        city = params.get("city", "Tashkent")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/current",
                params={"city": city, "key": self.api_key},
            ) as resp:
                data = await resp.json()
                return json.dumps(data, ensure_ascii=False)
```

### 3-qadam: Pluginni sozlang

`config.json` ga plugin qo'shing:

```json
{
  "plugins": [
    {
      "name": "weather",
      "enabled": true,
      "config": {
        "api_key": "your-weather-api-key",
        "base_url": "https://api.weather.example.com"
      }
    }
  ]
}
```

## @tool dekoratori

`@tool` dekoratori metodga agent tomonidan chaqirilish imkonini beradi:

```python
@tool(
    name="tool_name",           # Noyob tool nomi
    description="What it does", # LLM ga ko'rsatiladi
    parameters={                # Kirish uchun JSON Schema
        "type": "object",
        "required": ["param1"],
        "properties": {
            "param1": {"type": "string", "description": "..."},
            "param2": {"type": "integer", "description": "...", "default": 10},
        },
    },
)
async def my_tool(self, params: dict) -> str:
    # params -- JSON Schema ga mos dict
    # String qaytaring (odatda JSON)
    return json.dumps({"result": "value"})
```

`Plugin` klassidagi `_collect_tools()` metodi barcha `@tool` bilan belgilangan metodlarni topib, `ToolDef` obyektlarini qaytaradi.

## Plugin hayot sikli

### Yuklash

1. Plugin nomi o'rnatilgan va tashqi papkalardan qidiriladi
2. `plugin.py` dinamik import qilinadi
3. `QanotPlugin` nomli klass qidiriladi; topilmasa, har qanday `Plugin` subklassi ishlatiladi
4. Klass yaratiladi va `setup(config)` chaqiriladi
5. `get_tools()` chaqirilib, har bir tool `ToolRegistry` ga ro'yxatdan o'tkaziladi
6. `tools_md` mazmuni `workspace/TOOLS.md` ga qo'shiladi
7. `soul_append` mazmuni `workspace/SOUL.md` ga qo'shiladi

### Ishlash vaqtida

- Toollar yuklangandan so'ng darhol ishga tayyor
- Plugin instansiyasi jarayon yashayotgan vaqt davomida saqlanadi
- Agent tool chaqirganda handler params dict bilan ishga tushadi

### Yopilish

Jarayon tugaganda `teardown()` chaqiriladi. Ulanishlarni yopish, buferlarni flush qilish va h.k. uchun ishlating.

## TOOLS.md integratsiyasi

Plugin `tools_md` o'rnatgan bo'lsa, bu mazmun workspace `TOOLS.md` fayliga qo'shiladi. Agent toollaringiz haqida aynan shu orqali bilib oladi -- mazmun system prompt da ko'rinadi.

Mazmun faqat bir marta qo'shiladi (plugin nomi bo'yicha tekshiriladi). Agentga toollaringizni qachon va qanday ishlatishni tushuntiruvchi Markdown yozing.

## SOUL_APPEND integratsiyasi

Plugin `soul_append` o'rnatgan bo'lsa, bu mazmun workspace `SOUL.md` fayliga qo'shiladi. Pluginingizga oid shaxsiy xususiyatlar yoki xulq-atvor qoidalarini qo'shish uchun ishlating.

`soul_append` ning birinchi qatori takrorlanmaslik markeri sifatida ishlatiladi -- ikki marta qo'shilmaydi.

## Plugin konfiguratsiyasi

`setup()` ga berilgan `config` dict to'g'ridan-to'g'ri `config.json` dagi plugin yozuvidan keladi. Istalgan kalit-qiymat juftlarini qo'yishingiz mumkin:

```json
{
  "name": "myplugin",
  "enabled": true,
  "config": {
    "api_url": "https://api.example.com",
    "db_host": "localhost",
    "db_port": 3306,
    "db_user": "admin",
    "db_password": "secret",
    "timeout": 30
  }
}
```

`setup()` da foydalanish:

```python
async def setup(self, config: dict) -> None:
    self.api_url = config["api_url"]
    self.timeout = config.get("timeout", 10)
```

## To'g'ridan-to'g'ri tool ro'yxatdan o'tkazish

To'liq plugin kerak bo'lmagan hollarda, toollarni bevosita `ToolRegistry` ga ro'yxatdan o'tkazing:

```python
async def my_handler(params: dict) -> str:
    return json.dumps({"ok": True})

registry.register(
    name="my_tool",
    description="Does something useful.",
    parameters={"type": "object", "properties": {}},
    handler=my_handler,
)
```

Bu `qanot/main.py` da o'rnatilgan toollar uchun ishlatiladi va maxsus kirish nuqtalarida ham ishlatish mumkin.

## Plugin topish mexanizmi

Pluginlar papka nomi bo'yicha topiladi. Loader quyidagilarni tekshiradi:

1. `{package_root}/plugins/{name}/plugin.py` -- Qanot bilan birga keladigan o'rnatilgan pluginlar
2. `{plugins_dir}/{name}/plugin.py` -- config yo'lidagi tashqi pluginlar

Plugin papkasi yuklash vaqtida vaqtincha `sys.path` ga qo'shiladi, keyin olib tashlanadi. Shuning uchun pluginingiz o'z papkasidagi qo'shni modullardan import qilishi mumkin.

## Plugin Manifest (plugin.json)

Pluginlar metadata va dependency boshqaruvi uchun `plugin.json` faylini o'z ichiga olishi mumkin:

```json
{
  "name": "weather",
  "version": "1.0.0",
  "description": "Weather information for Uzbekistan cities",
  "author": "Your Name",
  "dependencies": ["aiohttp>=3.9"],
  "plugin_deps": ["cloud_reporter"],
  "required_config": ["api_key"],
  "min_qanot_version": "2.0.0",
  "homepage": "https://github.com/example/weather-plugin",
  "license": "MIT"
}
```

| Maydon | Turi | Tavsif |
|--------|------|--------|
| `name` | string | Plugin nomi (standart: papka nomi) |
| `version` | string | Semantik versiya (standart: `"0.1.0"`) |
| `description` | string | Odam tushunadigan tavsif |
| `author` | string | Plugin muallifi |
| `dependencies` | list | Plugin uchun kerakli pip paketlar |
| `plugin_deps` | list | Bu plugin bog'liq bo'lgan boshqa Qanot pluginlar |
| `required_config` | list | Plugin configida bo'lishi shart bo'lgan kalitlar |
| `min_qanot_version` | string | Talab qilinadigan minimal Qanot versiyasi |
| `homepage` | string | Plugin hujjatlari yoki repozitoriya URL |
| `license` | string | Litsenziya identifikatori (standart: `"MIT"`) |

Agar `plugin.json` bo'lmasa, papka nomidan standart manifest yaratiladi.

## Xatolarni boshqarish

### on_error() hook

Pluginlar tool bajarilish xatolarini boshqarish uchun `on_error()` metodini qayta yozishi mumkin:

```python
async def on_error(self, tool_name: str, error: Exception) -> None:
    """Tool bajarilishi muvaffaqiyatsiz bo'lganda chaqiriladi."""
    logger.error("Tool %s failed: %s", tool_name, error)
    # Maxsus xato boshqaruvi: qayta urinish, xabar berish, zaxira variant va h.k.
```

Bu hook tool nomi va yuzaga kelgan istisno bilan chaqiriladi. Maxsus xato hisoboti, qayta urinish mantiqiy yoki muammosiz pasayish uchun uni qayta yozing.

### validate_tool_params()

`validate_tool_params()` funksiyasi tool parametrlari uchun yengil JSON Schema validatsiyasini ta'minlaydi:

```python
from qanot.plugins.base import validate_tool_params

errors = validate_tool_params(
    params={"city": "Tashkent", "units": 42},
    schema={
        "type": "object",
        "required": ["city"],
        "properties": {
            "city": {"type": "string"},
            "units": {"type": "string"},
        },
    },
)
# errors: ["Parameter 'units' expected string, got int"]
```

Bu majburiy maydonlar va asosiy tur mosligini tekshiradi (string, integer, number, boolean, array, object). Barcha parametrlar to'g'ri bo'lsa bo'sh ro'yxat qaytaradi.

## Mavjud pluginlar

| Plugin | Toollar | Tavsif |
|--------|---------|--------|
| amoCRM | 20 | CRM integratsiya: leadlar, kontaktlar, pipelinelar, vazifalar, izohlar |
| Bitrix24 | 24 | CRM integratsiya: bitimlar, leadlar, kontaktlar, vazifalar, faoliyatlar |
| 1C Enterprise | 13 | Buxgalteriya: kontragentlar, mahsulotlar, sotuvlar, xaridlar, qoldiqlar |
| AbsMarket | 8 | POS tizimi: mahsulotlar, sotuvlar, inventar, hisobotlar |
| AbsVision | 3 | HR tizimi: xodimlar, davomat, ish haqi |
| iBox POS | 10 | POS tizimi: mahsulotlar, buyurtmalar, to'lovlar, qoldiqlar |
| Eskiz SMS | 4 | SMS yuborish, shablon boshqaruvi, yetkazish holati (Eskiz.uz orqali) |
| MySQL Query | 1 | Mustaqil faqat-SELECT SQL so'rov tooli |
| Cloud Reporter | 1 | Qanot Cloud platformasiga foydalanish hisoboti |

Ko'proq pluginlar [QanotHub](https://hub.qanot.ai) da mavjud -- jamoat va rasmiy pluginlar katalogi.
