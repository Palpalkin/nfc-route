"""Личный помощник для обхода заведений: бот + Mini App + API (OSM, без ключей)."""
import asyncio, json, math, os, sqlite3, urllib.parse
from collections import Counter
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo)
from dotenv import load_dotenv

load_dotenv()
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

async def overpass(s, lat, lon, rad):
    q = f"""[out:json][timeout:40];(
    nwr(around:{rad},{lat},{lon})[shop~"^({SHOP})$"][name];
    nwr(around:{rad},{lat},{lon})[amenity~"^({AMEN})$"][name];
    nwr(around:{rad},{lat},{lon})[craft~"^({CRAFT})$"][name];
    );out center tags;"""
    async with s.post("https://overpass-api.de/api/interpreter", data={"data": q}, headers=UA) as r:
        return (await r.json())["elements"]

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
    d = await req.json()
    async with ClientSession() as s:
        center = (d["lat"], d["lon"]) if "lat" in d else await geocode(s, d["q"])
        if not center: return web.json_response({"error": "Не нашёл такое место"}, status=404)
        els = await overpass(s, *center, int(d.get("radius", 1200)))
    res = build(els, center, int(d.get("limit", 40))); res["center"] = center
    return web.json_response(res)

async def api_visit(req):
    d = await req.json()
    db.execute("insert into visits(osm_id,status,note) values(?,?,?) on conflict(osm_id) do update set status=excluded.status, note=excluded.note",
               (d["id"], d["status"], d.get("note", "")))
    db.commit(); return web.json_response({"ok": True})

# --- бот ---
dp = Dispatcher()
def kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗺 Открыть карту", web_app=WebAppInfo(url=WEBAPP_URL))]])

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("Открой карту, введи район или нажми «Я здесь» — соберу маршрут.", reply_markup=kb())

@dp.message(F.text)
async def text(m: Message):
    await m.answer(f"Район «{m.text}» — открой карту и введи его там.", reply_markup=kb())

async def main():
    app = web.Application()
    app.add_routes([web.post("/api/search", api_search), web.post("/api/visit", api_visit),
                    web.get("/", lambda r: web.FileResponse("static/index.html"))])
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await dp.start_polling(Bot(BOT_TOKEN))

if __name__ == "__main__":
    asyncio.run(main())
