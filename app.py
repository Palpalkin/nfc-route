"""Личный помощник для обхода заведений: бот + Mini App + API (OSM, без ключей)."""
import asyncio, json, logging, math, os, sqlite3, urllib.parse
from collections import Counter
from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
BOT_TOKEN, WEBAPP_URL = os.environ["BOT_TOKEN"], os.environ["WEBAPP_URL"].rstrip("/")
PORT = int(os.getenv("PORT", 8080))
UA = {"User-Agent": "nfc-route-personal/1.0"}
VISIT_MIN = 4          # минут на заведение
WALK_KMH = 4.8

db = sqlite3.connect("visits.db", check_same_thread=False)
db.execute("create table if not exists visits(osm_id text primary key, status text, note text, ts default current_timestamp)")

# --- категории, где обычно сидит владелец ---
SHOP = "hairdresser|beauty|barber|mobile_phone|florist|bakery|butcher|clothes|shoes|jewelry|optician|tailor|copyshop|pet|gift|variety_store|car_repair|car_parts|tyres"
AMEN = "cafe|fast_food|dentist|veterinary|car_wash|laundry"
CRAFT = "shoemaker|tailor|photographer|electronics_repair|key_cutter"
BAD_WORDS = ("цоколь", "подвал", "бизнес-центр", "бц ", "офис")

def hav(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1]); dp = p2 - p1
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

async def geocode(s, q):
    async with s.get("https://nominatim.openstreetmap.org/search",
                     params={"q": q, "format": "json", "limit": 1}, headers=UA) as r:
        j = await r.json()
    return (float(j[0]["lat"]), float(j[0]["lon"])) if j else None

OVERPASS_URLS = (
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)

class SearchError(Exception):
    def __init__(self, msg, code=502):
        super().__init__(msg); self.code = code

async def overpass(s, lat, lon, rad):
    q = f"""[out:json][timeout:18];(
    nwr(around:{rad},{lat},{lon})[shop~"^({SHOP})$"][name];
    nwr(around:{rad},{lat},{lon})[amenity~"^({AMEN})$"][name];
    nwr(around:{rad},{lat},{lon})[craft~"^({CRAFT})$"][name];
    );out center tags;"""

    async def one(url):
        async with s.post(url, data={"data": q}, headers=UA, timeout=ClientTimeout(total=20)) as r:
            j = await r.json(content_type=None)
        if "elements" not in j:
            raise ValueError(f"{url}: ответ без elements")
        return j["elements"]

    # все зеркала параллельно, берём первый удачный ответ
    tasks = [asyncio.create_task(one(u)) for u in OVERPASS_URLS]
    try:
        for fut in asyncio.as_completed(tasks):
            try:
                return await fut
            except Exception as e:
                logging.warning("Overpass: %r", e)
    finally:
        for t in tasks:
            t.cancel()
    raise SearchError("Сервер карт не отвечает, попробуйте через минуту.")

async def find(q=None, lat=None, lon=None, radius=1200, limit=40):
    async with ClientSession(timeout=ClientTimeout(total=120)) as s:
        if lat is not None:
            center = (lat, lon)
        else:
            try:
                center = await geocode(s, q)
            except Exception as e:
                logging.warning("Nominatim: %r", e)
                raise SearchError("Не удалось определить место, попробуйте позже.")
            if not center:
                raise SearchError("Не нашёл такое место. Напишите, например: Казань, Баумана", 404)
        els = await overpass(s, *center, radius)
    res = build(els, center, limit); res["center"] = center
    return res

def build(elements, center, limit):
    names = Counter(e["tags"]["name"].lower() for e in elements)
    out, dropped = [], Counter()
    for e in elements:
        t = e["tags"]; c = e.get("center") or e
        if "lat" not in c: continue
        name = t["name"]
        addr = " ".join(filter(None, [t.get("addr:street"), t.get("addr:housenumber")]))
        txt = (name + " " + addr + " " + t.get("description", "")).lower()
        if names[name.lower()] >= 2 or "brand" in t or "operator" in t: dropped["сети"] += 1; continue
        if t.get("level") in ("-1", "-2") or t.get("location") == "underground" or any(w in txt for w in BAD_WORDS):
            dropped["подвалы/офисы"] += 1; continue
        if t.get("appointment") == "only": dropped["по записи"] += 1; continue
        out.append(dict(id=f'{e["type"]}{e["id"]}', name=name, lat=c["lat"], lon=c["lon"],
                        addr=addr or "адрес не указан", cat=t.get("shop") or t.get("amenity") or t.get("craft"),
                        hours=t.get("opening_hours", ""), site=t.get("website") or t.get("contact:website", ""),
                        phone=t.get("phone") or t.get("contact:phone", ""), bld=addr))
    # уже обработанные — не показываем
    done = {r[0] for r in db.execute("select osm_id from visits where status in ('Поставили','Отказ','Не показывать больше')")}
    out = [o for o in out if o["id"] not in done]
    # маршрут: nearest neighbor от центра, здание считаем одной точкой
    route, cur, rest = [], center, out[:]
    while rest and len(route) < limit:
        nxt = min(rest, key=lambda o: hav(cur, (o["lat"], o["lon"])))
        same = [o for o in rest if o["bld"] and o["bld"] == nxt["bld"]] or [nxt]
        for o in same: route.append(o); rest.remove(o)
        cur = (nxt["lat"], nxt["lon"])
    km = sum(hav(a, b) for a, b in zip([center]+[(o["lat"], o["lon"]) for o in route],
                                         [(o["lat"], o["lon"]) for o in route]))
    minutes = round(km/WALK_KMH*60 + len(route)*VISIT_MIN)
    pts = "~".join(f'{o["lat"]},{o["lon"]}' for o in route[:10])
    return dict(found=len(elements), dropped=dropped, route=route, km=round(km, 1), minutes=minutes,
                buildings=len({o["bld"] or o["id"] for o in route}),
                ymaps=f"https://yandex.ru/maps/?rtext={center[0]},{center[1]}~{pts}&rtt=pd")

# --- API ---
async def api_search(req):
    try:
        d = await req.json()
        res = await find(d.get("q"), d.get("lat"), d.get("lon"),
                         int(d.get("radius", 1200)), int(d.get("limit", 40)))
        return web.json_response(res)
    except SearchError as e:
        return web.json_response({"error": str(e)}, status=e.code)
    except Exception:
        logging.exception("api_search failed")
        return web.json_response({"error": "Что-то пошло не так, попробуйте ещё раз."}, status=500)

async def api_visit(req):
    d = await req.json()
    db.execute("insert into visits(osm_id,status,note) values(?,?,?) on conflict(osm_id) do update set status=excluded.status, note=excluded.note",
               (d["id"], d["status"], d.get("note", "")))
    db.commit(); return web.json_response({"ok": True})

# --- бот ---
dp = Dispatcher()

def kb(ymaps=None):
    rows = []
    if ymaps:
        rows.append([InlineKeyboardButton(text="🧭 Маршрут в Яндекс Картах", url=ymaps)])
    rows.append([InlineKeyboardButton(text="🗺 Открыть карту", web_app=WebAppInfo(url=WEBAPP_URL))])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def reply_text(res):
    r = res["route"]
    if not r:
        return "Подходящих заведений здесь не нашёл. Попробуйте другой район или улицу."
    h, m = divmod(res["minutes"], 60)
    lines = [f"{len(r)} мест за {h} ч {m} мин · {res['km']} км пешком, {res['buildings']} зданий.", ""]
    for i, o in enumerate(r[:15], 1):
        lines.append(f"{i}. {o['name']} — {o['addr']}")
    if len(r) > 15:
        lines.append(f"…и ещё {len(r) - 15} в приложении")
    return "\n".join(lines)

async def run_search(m: Message, **kw):
    wait = await m.answer("Ищу заведения…")
    try:
        res = await find(**kw)
        await wait.edit_text(reply_text(res), reply_markup=kb(res["ymaps"] if res["route"] else None))
    except SearchError as e:
        await wait.edit_text(str(e), reply_markup=kb())
    except Exception:
        logging.exception("bot search failed")
        await wait.edit_text("Что-то пошло не так, попробуйте ещё раз.", reply_markup=kb())

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("Напишите район или улицу, например «Казань, Баумана», или отправьте геопозицию — соберу маршрут. "
                   "Карту тоже можно открыть кнопкой ниже.", reply_markup=kb())

@dp.message(F.location)
async def loc(m: Message):
    await run_search(m, lat=m.location.latitude, lon=m.location.longitude)

@dp.message(F.text, ~F.text.startswith("/"))
async def text(m: Message):
    await run_search(m, q=m.text.strip())

async def main():
    app = web.Application()
    app.add_routes([web.post("/api/search", api_search), web.post("/api/visit", api_visit),
                    web.get("/", lambda r: web.FileResponse("static/index.html"))])
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await dp.start_polling(Bot(BOT_TOKEN))

if __name__ == "__main__":
    asyncio.run(main())
