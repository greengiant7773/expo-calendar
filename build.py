#!/usr/bin/env python3
"""5会場の展示会・イベント情報を集めて docs/events.json を作る。

  python build.py --months 2   通常実行
  python build.py --dump       取得したHTMLを debug/ に保存（調整用）

会場ごとの取得方法
  東京ビッグサイト … 公式PDF（3ヶ月分）を pdfplumber で解析。失敗時はHTML一覧
  幕張メッセ       … /event/?month=YYYYMM&page=N を月ごと・ページごとに取得
  パシフィコ横浜   … 公式サイトが公開している Googleカレンダー(iCal) 6本を取得
  東京国際フォーラム … /visitors/event/?year=YYYY&month=M を月ごとに取得
  浜松商工会議所   … /events/ の月別テーブルを取得
専用パーサが 0件のときは汎用パーサ harvest() にフォールバックする。
"""
import argparse
import csv
import io
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
OUT = ROOT / "docs" / "events.json"
MANUAL = ROOT / "manual" / "events.csv"
JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; ExpoCalendarBot/1.0; internal use)"
TIMEOUT = 30
log = logging.getLogger("expo")

# パシフィコ横浜が公式サイト（/eventInfo）で公開しているカテゴリ別 Googleカレンダー
PACIFICO_CALENDARS = {
    "学会・会議": "230f1639b8d54499f15caa874711a36b626933721072c1f9182d1282e4ddc586",
    "大会・式典・その他": "9e043fe7408716fce55f5f54f484074002eb939a192acdb50237cfb3ffac54fb",
    "イベント・コンサート": "78a66c66587ef8c39d987a3eb014082df0eff54f2445e9f837f94404fa0304e6",
    "展示会・見本市": "6804e72a022c5b213349f94093988a11b91248a693afc0a30328b839ea2ed120",
    "物販・展示即売会": "737b19946fd8b839a17aaaa8ddc15f64af59045e36622b78fb50dcc31959992e",
    "公開講座": "1d7858363ffb40c9c82e272fc18b92528e0db30df3d6e159dfc503eeee6464a5",
}
PACIFICO_ICAL = ("https://calendar.google.com/calendar/ical/"
                 "{id}%40group.calendar.google.com/public/basic.ics")

VENUES = [
    {"key": "bigsight", "name": "東京ビッグサイト", "scope": None,
     "url": "https://www.bigsight.jp/visitor/event/",
     "pdf_index": "https://www.bigsight.jp/visitor/event/calendar.html"},
    {"key": "makuhari", "name": "幕張メッセ", "scope": "section.eventCont",
     "url": "https://www.m-messe.co.jp/event/"},
    {"key": "pacifico", "name": "パシフィコ横浜", "scope": None,
     "url": "https://www.pacifico.co.jp/eventInfo"},
    {"key": "forum", "name": "東京国際フォーラム", "scope": "main",
     "url": "https://www.t-i-forum.co.jp/visitors/event/"},
    {"key": "hamamatsu", "name": "浜松商工会議所", "scope": "#contents",
     "url": "https://www.hamamatsu-cci.or.jp/events/"},
]

# ---------------------------------------------------------------- 日付の解釈

_ZEN = str.maketrans("０１２３４５６７８９（）～－", "0123456789()~-")
DASH = r"[~〜～\-–—ー−]"
PAT_FULL = re.compile(
    r"(?P<y1>\d{4})\s*[年./\-]\s*(?P<m1>\d{1,2})\s*[月./\-]\s*(?P<d1>\d{1,2})\s*日?"
    r"(?:\s*\([^)]{0,6}\))?"
    r"(?:\s*" + DASH + r"\s*(?:(?P<y2>\d{4})\s*[年./\-]\s*)?"
    r"(?:(?P<m2>\d{1,2})\s*[月./\-]\s*)?(?P<d2>\d{1,2})\s*日?)?")
PAT_MD = re.compile(
    r"(?<!\d)(?P<m1>\d{1,2})\s*[月/]\s*(?P<d1>\d{1,2})\s*日?"
    r"(?:\s*\([^)]{0,6}\))?"
    r"(?:\s*" + DASH + r"\s*(?:(?P<m2>\d{1,2})\s*[月/]\s*)?(?P<d2>\d{1,2})\s*日?)?")
PAT_YMD = re.compile(r"(?P<y>\d{4})\s*[年./\-]\s*(?P<m>\d{1,2})\s*[月./\-]\s*(?P<d>\d{1,2})")
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def _safe(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_range(text, base_year=None, base_month=None):
    """テキストから日付範囲を取り出して (開始, 終了) を返す。無ければ (None, None)。"""
    if not text:
        return None, None
    t = str(text).translate(_ZEN)
    base_year = base_year or date.today().year

    m = PAT_FULL.search(t)
    if m:
        y1 = int(m.group("y1"))
        start = _safe(y1, int(m.group("m1")), int(m.group("d1")))
        if not start:
            return None, None
        if m.group("d2"):
            y2 = int(m.group("y2")) if m.group("y2") else y1
            mo2 = int(m.group("m2")) if m.group("m2") else start.month
            if not m.group("y2") and mo2 < start.month:
                y2 = y1 + 1
            end = _safe(y2, mo2, int(m.group("d2"))) or start
        else:
            end = start
        return start, (end if end >= start else start)

    m = PAT_MD.search(t)
    if m:
        mo1 = int(m.group("m1"))
        if not 1 <= mo1 <= 12:
            return None, None
        year = base_year
        if base_month and mo1 < base_month - 6:
            year += 1
        start = _safe(year, mo1, int(m.group("d1")))
        if not start:
            return None, None
        if m.group("d2"):
            mo2 = int(m.group("m2")) if m.group("m2") else mo1
            end = _safe(year + 1 if mo2 < mo1 else year, mo2, int(m.group("d2"))) or start
        else:
            end = start
        return start, (end if end >= start else start)

    return None, None


def all_dates(text):
    """テキスト中の「YYYY年M月D日」形式の日付をすべて返す（浜松の複数日程セル用）。"""
    out = []
    for m in PAT_YMD.finditer(str(text).translate(_ZEN)):
        d = _safe(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        if d and d not in out:
            out.append(d)
    return out


def month_iter(start, months):
    """start の月から months+1 ヶ月ぶんの (年, 月) を返す。"""
    y, m = start.year, start.month
    for _ in range(months + 1):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def clean(text):
    return " ".join(str(text or "").split())


def event(venue, title, start, end, url, category=None):
    e = {"venue": venue, "title": clean(title)[:120],
         "start": start.isoformat(), "end": (end or start).isoformat(), "url": url}
    if category:
        e["category"] = clean(category)
    return e


# ------------------------------------------------------------------- 取得

NOISE = re.compile(
    r"(アクセス|駐車場|お問合せ|お問い合わせ|プライバシー|サイトマップ|"
    r"ご利用案内|よくある|採用|会社概要|ホーム|トップ|一覧へ|詳しく|"
    r"Cookie|ログイン|検索)")
DATE_STR = re.compile(r"[\d０-９]{1,4}\s*[年月/.\-]\s*[\d０-９]{1,2}\s*日?(\s*\([^)]{0,6}\))?")
TIME_STR = re.compile(r"\d{1,2}:\d{2}")


def get(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r


def harvest(html, base_url, venue, scope, base_year, base_month):
    """日付を含むブロックからイベント候補を拾う汎用パーサ（フォールバック用）。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    root = soup.select_one(scope) if scope else soup
    if root is None:
        root = soup

    out, seen = [], set()
    for block in root.select("li, tr, article, .event, .eventItem, div"):
        text = clean(block.get_text(" ", strip=True))
        if not (10 <= len(text) <= 400):
            continue
        start, end = parse_range(text, base_year, base_month)
        if not start:
            continue
        # 入れ子で同じ内容を重複して拾わない
        if any(parse_range(clean(c.get_text(" ", strip=True)), base_year, base_month)[0]
               for c in block.find_all(["li", "tr", "article"], recursive=True)):
            continue
        link = block.find("a", href=True)
        url = urljoin(base_url, link["href"]) if link else base_url
        title = clean(link.get_text(" ", strip=True)) if link else ""
        if len(title) < 4:
            title = clean(DATE_STR.sub(" ", text))[:80]
        if not title or NOISE.search(title):
            continue
        key = (title[:40], start.isoformat())
        if key in seen:
            continue
        seen.add(key)
        out.append(event(venue, title, start, end, url))
    return out


# ---- 東京ビッグサイト ---------------------------------------------------

BIGSIGHT_SKIP = re.compile(r"(掲載範囲|作成日|催事名|主催者名|問合せ|問い合わせ)")


def bigsight_line_ok(line, title):
    """PDFの1行が催事の行かどうか。見出し・注記・時刻だけの行を落とす。"""
    if BIGSIGHT_SKIP.search(line):
        return False
    if TIME_STR.match(line):            # 「10:00~17:00, …」で始まる行は前の催事の続き
        return False
    body = TIME_STR.sub("", title)
    body = re.sub(r"[\s,、。()（）~〜～\-–—・/／:：]", "", body)
    if len(body) < 4:                   # 日付と時刻・記号を除いて何も残らない
        return False
    if re.fullmatch(r"[\d\s,.:~〜～\-]+", body):
        return False
    return True


def fetch_bigsight(cfg, y, mo):
    """公式の3ヶ月分PDFを優先。だめならHTML一覧にフォールバック。"""
    events = []
    try:
        soup = BeautifulSoup(get(cfg["pdf_index"]).text, "html.parser")
        pdf = next((urljoin(cfg["pdf_index"], a["href"])
                    for a in soup.find_all("a", href=True)
                    if a["href"].lower().endswith(".pdf")), None)
        if pdf:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(get(pdf).content)) as doc:
                lines = []
                for page in doc.pages:
                    lines += (page.extract_text() or "").splitlines()
            for line in lines:
                line = clean(line)
                if len(line) < 8:
                    continue
                start, end = parse_range(line, y, mo)
                if not start:
                    continue
                title = clean(DATE_STR.sub(" ", line))
                title = re.sub(r"(?:^|(?<=\s))[~〜～\-‐－–—]+(?=\s|$)", "", title)  # 日付を抜いた跡の「～」「- 」
                title = clean(title)[:80]
                if not title or NOISE.search(title) or not bigsight_line_ok(line, title):
                    continue
                events.append(event(cfg["name"], title, start, end, pdf))
    except Exception as e:
        log.warning("ビッグサイトPDFの取得に失敗: %s", e)
    if not events:
        events = harvest(get(cfg["url"]).text, cfg["url"], cfg["name"],
                         cfg["scope"], y, mo)
    return events


# ---- 幕張メッセ ---------------------------------------------------------

def parse_makuhari(html, base_url, venue):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.eventInr"):
        a = li.find("a", href=True)
        d = li.select_one(".date")
        t = li.select_one(".eventTit")
        if not (a and d and t):
            continue
        start, end = parse_range(d.get_text(" ", strip=True))
        if not start:
            continue
        cat = li.select_one(".category")
        out.append(event(venue, t.get_text(" ", strip=True), start, end,
                         urljoin(base_url, a["href"]),
                         cat.get_text(" ", strip=True) if cat else None))
    return out


def fetch_makuhari(cfg, today, months, dump_dir=None):
    events, seen = [], set()
    for y, m in month_iter(today, months):
        for page in range(1, 11):
            url = f"{cfg['url']}?month={y}{m:02d}&page={page}"
            res = get(url)
            if dump_dir:
                (dump_dir / f"makuhari_{y}{m:02d}_{page}.html").write_text(res.text, encoding="utf-8")
            batch = parse_makuhari(res.text, cfg["url"], cfg["name"])
            new = [e for e in batch if e["url"] not in seen]
            seen.update(e["url"] for e in new)
            events += new
            if len(batch) < 20:        # 1ページ20件。それ未満なら最終ページ
                break
    return events


# ---- パシフィコ横浜（Googleカレンダー iCal） ------------------------------

def parse_ics(text):
    """iCal を VEVENT ごとの dict にする（折り返し行を結合、最低限の項目のみ）。"""
    lines = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    events, cur = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}
        elif ln == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in ln:
            key, val = ln.split(":", 1)
            cur[key.split(";")[0]] = (val.replace("\\,", ",").replace("\\;", ";")
                                      .replace("\\n", " ").replace("\\\\", "\\"))
    return events


def _ics_date(val):
    val = (val or "").strip()
    if len(val) >= 8 and val[:8].isdigit():
        return _safe(int(val[:4]), int(val[4:6]), int(val[6:8]))
    return None


def fetch_pacifico(cfg, today, horizon):
    events = []
    for category, cal_id in PACIFICO_CALENDARS.items():
        try:
            text = get(PACIFICO_ICAL.format(id=cal_id)).text
        except Exception as e:
            log.warning("パシフィコ横浜(%s)の取得に失敗: %s", category, e)
            continue
        for ev in parse_ics(text):
            start = _ics_date(ev.get("DTSTART"))
            if not start:
                continue
            end = _ics_date(ev.get("DTEND")) or start
            # 終日イベントの DTEND は「翌日」を指すので1日戻す
            if "T" not in ev.get("DTEND", "") and end > start:
                end -= timedelta(days=1)
            if end < today or start > horizon:
                continue
            title = clean(ev.get("SUMMARY", ""))
            if not title:
                continue
            m = re.search(r'href="([^"]+)"', ev.get("DESCRIPTION", ""))
            url = m.group(1).strip() if m and m.group(1).strip() else cfg["url"]
            events.append(event(cfg["name"], title, start, end, url, category))
    return events


# ---- 東京国際フォーラム -------------------------------------------------

def _merge_ranges(events):
    """同じ詳細ページ＋同じ名称の催事を1件にまとめ、会期を広げる。"""
    merged = {}
    for e in events:
        k = (e["url"], e["title"])
        if k in merged:
            merged[k]["start"] = min(merged[k]["start"], e["start"])
            merged[k]["end"] = max(merged[k]["end"], e["end"])
        else:
            merged[k] = dict(e)
    return list(merged.values())


def parse_forum(html, base_url, venue):
    """日付ごとの li から催事を拾う。複数日にまたがる催事は _merge_ranges で結合する。"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.p-newsGroup__item"):
        dt = li.find("dt")
        if not dt:
            continue
        days = all_dates(dt.get("aria-label") or dt.get_text(" ", strip=True))
        if not days:
            continue
        day = days[0]
        for box in li.select(".p-news__detail"):
            a = box.find("a", href=True)
            if not a:
                continue
            title = clean(a.get_text(" ", strip=True))
            if not title:
                continue
            holder = box.find_parent(class_="js-sort-item")
            tag = holder.select_one(".c-tag") if holder else None
            out.append(event(venue, title, day, day, urljoin(base_url, a["href"]),
                             tag.get_text(" ", strip=True) if tag else None))
    return _merge_ranges(out)


def fetch_forum(cfg, today, months, dump_dir=None):
    events = []
    for y, m in month_iter(today, months):
        res = get(f"{cfg['url']}?year={y}&month={m}")
        if dump_dir:
            (dump_dir / f"forum_{y}{m:02d}.html").write_text(res.text, encoding="utf-8")
        events += parse_forum(res.text, cfg["url"], cfg["name"])
    return _merge_ranges(events)   # 月をまたぐ催事を結合


# ---- 浜松商工会議所 -----------------------------------------------------

def parse_hamamatsu(html, base_url, venue):
    """月別テーブルの各行から催事を拾う。複数日程の行は開催日ごとに1件にする。"""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for tr in soup.select("table.c_tbl1 tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        a = tds[1].find("a", href=True)
        if not a:
            continue
        title = clean(a.get_text(" ", strip=True))
        url = urljoin(base_url, a["href"])
        cats = [clean(c.get_text(" ", strip=True)) for c in tr.select(".cat")]
        cat = "・".join(c for c in cats if c) or None
        for d in all_dates(tds[0].get_text(" ", strip=True)):
            k = (url, d)
            if k in seen:
                continue
            seen.add(k)
            out.append(event(venue, title, d, d, url, cat))
    return out


# ---- まとめ --------------------------------------------------------------

def fetch_all(months, dump_dir=None):
    today = date.today()
    horizon = today + timedelta(days=31 * months)
    events, status = [], []
    for cfg in VENUES:
        try:
            key = cfg["key"]
            if key == "bigsight":
                ev = fetch_bigsight(cfg, today.year, today.month)
            elif key == "makuhari":
                ev = fetch_makuhari(cfg, today, months, dump_dir)
            elif key == "pacifico":
                ev = fetch_pacifico(cfg, today, horizon)
            elif key == "forum":
                ev = fetch_forum(cfg, today, months, dump_dir)
            else:
                res = get(cfg["url"])
                if dump_dir:
                    (dump_dir / f"{key}.html").write_text(res.text, encoding="utf-8")
                ev = parse_hamamatsu(res.text, cfg["url"], cfg["name"]) if key == "hamamatsu" else []
                if not ev:
                    ev = harvest(res.text, cfg["url"], cfg["name"], cfg["scope"],
                                 today.year, today.month)
            if not ev and key in ("makuhari", "forum"):
                # 専用パーサが0件なら汎用パーサで拾ってみる
                ev = harvest(get(cfg["url"]).text, cfg["url"], cfg["name"], cfg["scope"],
                             today.year, today.month)
            ev = [e for e in ev if e["end"] >= today.isoformat()
                  and e["start"] <= horizon.isoformat()]
            events += ev
            status.append({"venue": cfg["name"], "count": len(ev), "error": None})
            log.info("%s: %d件", cfg["name"], len(ev))
        except Exception as e:
            status.append({"venue": cfg["name"], "count": 0, "error": str(e)[:200]})
            log.error("%s の取得に失敗: %s", cfg["name"], e)
    return events, status


def load_manual():
    """手入力ぶん（manual/events.csv）。ファイルが無ければ何もしない。"""
    if not MANUAL.exists():
        return []
    rows = []
    with MANUAL.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not (r.get("start") and r.get("title")):
                continue
            rows.append({"venue": r["venue"].strip(), "title": r["title"].strip(),
                         "start": r["start"].strip(),
                         "end": (r.get("end") or r["start"]).strip(),
                         "url": (r.get("url") or "").strip(), "manual": True})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=2)
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    dump_dir = None
    if args.dump:
        dump_dir = ROOT / "debug"
        dump_dir.mkdir(exist_ok=True)

    events, status = fetch_all(args.months, dump_dir)

    # 取得できなかった会場は前回のデータを残す
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    prev_events = prev.get("events", [])
    for s in status:
        if s["count"] == 0:
            kept = [e for e in prev_events
                    if e["venue"] == s["venue"] and not e.get("manual")]
            if kept:
                events += kept
                s["error"] = (s["error"] or "0件") + " / 前回のデータを表示中"

    events += load_manual()

    uniq, seen = [], set()
    for e in sorted(events, key=lambda x: (x["start"], x["venue"], x["title"])):
        k = (e["venue"], e["title"][:40], e["start"])
        if k in seen:
            continue
        seen.add(k)
        e["wd"] = WEEKDAYS[datetime.strptime(e["start"], "%Y-%m-%d").weekday()]
        uniq.append(e)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
                               "status": status, "events": uniq},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(uniq)}件 -> {OUT}")
    for s in status:
        print(f"  {'OK ' if s['count'] else 'NG '}{s['venue']}: {s['count']}件 {s['error'] or ''}")


if __name__ == "__main__":
    main()
