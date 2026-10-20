"""
Mamikos kost scraper (edukasi / portofolio, non-komersial).

Alur:
  1. python mamikos_scraper.py discover --area depok
       -> baca sitemap, simpan daftar URL detail kos area tsb ke data/raw/urls.txt
  2. python mamikos_scraper.py inspect --url <salah_satu_url>
       -> simpan HTML mentah + ringkasan JSON yang ditemukan, untuk cek struktur
  3. python mamikos_scraper.py crawl --limit 50
       -> ambil listing, simpan ke data/raw/listings.jsonl (bisa di-resume)

Catatan etika:
  - Cek robots.txt otomatis (path /admin/, /unsubscribe/, /webview/ dilarang).
  - Delay acak 2-5 detik, 1 request pada satu waktu.
  - Jangan simpan data pribadi (nama/nomor HP pemilik). Lihat PII_KEYS.
  - Jangan commit data/raw/ ke GitHub (tambahkan ke .gitignore).
"""
import argparse
import gzip
import json
import random
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://mamikos.com"
SITEMAP_KOST = f"{BASE}/sitemaps/sitemap/sitemap-index-detail-kost.xml"
UA = "Mozilla/5.0 (portfolio-research-bot; contact: GANTI_DENGAN_EMAILMU@example.com)"
DELAY = (2.0, 5.0)

RAW = Path("data/raw")
RAW.mkdir(parents=True, exist_ok=True)
URLS_FILE = RAW / "urls.txt"
OUT_FILE = RAW / "listings.jsonl"

# kunci yang TIDAK boleh disimpan (data pribadi)
PII_KEYS = {"owner", "owner_name", "owner_phone", "phone", "phone_number",
            "email", "whatsapp", "telephone", "contact", "agent"}

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "id-ID,id;q=0.9"})

def _load_rules(text):
    """Ambil aturan Allow/Disallow untuk 'User-agent: *' (longest-match, seperti Google)."""
    rules, applies = [], False
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if ":" not in line:
            continue
        k, v = [x.strip() for x in line.split(":", 1)]
        k = k.lower()
        if k == "user-agent":
            applies = (v == "*")
        elif applies and k in ("allow", "disallow") and v:
            rules.append((k, v))
    return rules


try:
    _r = session.get(f"{BASE}/robots.txt", timeout=15)
    ROBOT_RULES = _load_rules(_r.text) if _r.status_code == 200 else []
    print(f"[robots.txt] status {_r.status_code}, {len(ROBOT_RULES)} aturan dimuat")
except requests.RequestException as e:
    raise SystemExit(f"Gagal mengambil robots.txt ({e}). Cek koneksi lalu coba lagi.")


def can_fetch(url):
    path = "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url
    best_len, allowed = -1, True
    for kind, rule in ROBOT_RULES:
        if path.startswith(rule) and len(rule) > best_len:
            best_len, allowed = len(rule), (kind == "allow")
    return allowed


def polite_get(url, retries=3):
    if not can_fetch(url):
        print(f"[SKIP robots] {url}")
        return None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200:
                time.sleep(random.uniform(*DELAY))
                return r
            if r.status_code in (403, 429):
                print(f"[STOP] status {r.status_code} - kemungkinan diblokir/rate limit. Berhenti dulu.")
                raise SystemExit(1)
            print(f"[{r.status_code}] {url} (percobaan {attempt})")
        except requests.RequestException as e:
            print(f"[ERR] {e} (percobaan {attempt})")
        time.sleep(5 * attempt)
    return None


def parse_sitemap(url):
    r = polite_get(url)
    if r is None:
        return [], []
    content = r.content
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = ET.fromstring(content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sub = [e.text.strip() for e in root.findall("s:sitemap/s:loc", ns)]
    urls = [e.text.strip() for e in root.findall("s:url/s:loc", ns)]
    return sub, urls


# Depok juga nama kecamatan di Sleman (DIY). Buang slug yang menyebut wilayah lain.
EXCLUDE_WORDS = ("sleman", "yogyakarta", "jogja", "bantul", "bandung",
                 "kulon-progo", "gunungkidul", "caturtunggal", "condong-catur")


def matches_area(url, area):
    """Cocokkan kota di AWAL slug (kost-kota-<area>- / kost-<area>-), bukan di nama kos,
    dan buang slug yang menyebut wilayah lain (mis. Depok, Sleman).
    """
    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    if not (slug.startswith(f"kost-kota-{area}-") or slug.startswith(f"kost-{area}-")):
        return False
    if area == "depok" and any(w in slug for w in EXCLUDE_WORDS):
        return False
    return True


def discover(area, max_sitemaps=None):
    """Kumpulkan URL detail kos untuk kota/area tertentu dari semua sitemap."""
    sub, urls = parse_sitemap(SITEMAP_KOST)
    queue = sub[:max_sitemaps] if max_sitemaps else sub
    print(f"Index berisi {len(sub)} sub-sitemap; memproses {len(queue)}.")
    found = [u for u in urls if matches_area(u, area)]
    for i, sm in enumerate(queue, 1):
        print(f"[sitemap {i}/{len(queue)}] {sm}  (cocok sejauh ini: {len(found)})")
        _, u = parse_sitemap(sm)
        found.extend(x for x in u if matches_area(x, area))
    found = sorted(set(found))
    URLS_FILE.write_text("\n".join(found), encoding="utf-8")
    print(f"Tersimpan {len(found)} URL ke {URLS_FILE}")


# Hanya field ini yang disimpan (whitelist). Data pemilik/kontak SENGAJA tidak ada.
KEEP_FIELDS = [
    "_id", "slug", "room_title", "area_city", "area_subdistrict", "gender",
    "latitude", "longitude", "size",
    "price_daily", "price_weekly", "price_monthly", "price_quarterly",
    "price_semiannualy", "price_yearly", "price_tag", "min_month",
    "fac_room", "fac_share", "fac_bath", "fac_near", "fac_park", "kos_rule",
    "rating", "review_count", "view_count", "love_count",
    "available_room", "updated_at", "not_updated", "online", "is_inactive_owner",
    "building_year", "is_booking", "booking_type", "level_name",
    "is_singgahsini", "is_apik", "is_elite", "goldplus",
    "number_success_kos_trx", "transaction_count", "is_flash_sale", "verification_status",
]


def parse_listing(html):
    """Ambil objek JS `var detail = {...};` dari halaman, lalu simpan field whitelist."""
    m = re.search(r"var\s+detail\s*=\s*", html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[m.end():])
    except json.JSONDecodeError:
        return None
    rec = {k: obj.get(k) for k in KEEP_FIELDS}
    rec["photo_count"] = len(obj.get("cards") or [])
    return rec


def inspect(url):
    r = polite_get(url)
    if r is None:
        return
    (RAW / "inspect_page.html").write_text(r.text, encoding="utf-8")  # ini jangan di-commit ajg
    rec = parse_listing(r.text)
    if rec is None:
        print("Variabel 'detail' tidak ditemukan. Struktur halaman mungkin berubah.")
        return
    print(json.dumps(rec, ensure_ascii=False, indent=2))


def crawl(limit):
    if not URLS_FILE.exists():
        raise SystemExit("Jalankan 'discover' dulu.")
    urls = URLS_FILE.read_text(encoding="utf-8").splitlines()
    done = set()
    if OUT_FILE.exists():
        for line in OUT_FILE.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["url"])
            except Exception:
                pass
    todo = [u for u in urls if u and u not in done][:limit]
    print(f"{len(done)} sudah ada, mengambil {len(todo)} baru.")

    fails = 0
    with OUT_FILE.open("a", encoding="utf-8") as f:
        for i, u in enumerate(todo, 1):
            r = polite_get(u)
            rec = parse_listing(r.text) if r is not None else None
            if rec is None:
                fails += 1
                print(f"[{i}/{len(todo)}] GAGAL parse {u}")
                if fails >= 10:
                    raise SystemExit("10 kegagalan berturut-turut/total. Berhenti, cek struktur halaman.")
                continue
            rec = {"url": u, "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"), **rec}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{i}/{len(todo)}] ok {rec['room_title']} | {rec['area_subdistrict']} | Rp{rec['price_monthly']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["discover", "inspect", "crawl"])
    ap.add_argument("--area", default="depok")
    ap.add_argument("--url")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--max-sitemaps", type=int, default=None)
    a = ap.parse_args()

    if a.mode == "discover":
        discover(a.area.lower(), a.max_sitemaps)
    elif a.mode == "inspect":
        inspect(a.url)
    else:
        crawl(a.limit)