#!/usr/bin/env python3
"""5会場の展示会・イベント情報を集めて docs/events.json を作る。

  python build.py --months 2   通常実行
  python build.py --dump       取得したHTMLを debug/ に保存（調整用）
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

VENUES = [
    {"key": "bigsight", "name": "東京ビッグサイト", "scope": None,
     "url": "https://www.bigsight.jp/visitor/event/",
     "pdf_index": "https://www.bigsight.jp/visitor/event/calendar.html"},
    {"key": "makuhari", "name": "幕張メッセ", "scope": None,
     "url": "https://www.m-messe.co.jp/event/"},
    {"key": "pacifico", "name": "パシフィコ横浜", "scope": None,
     "url": "https://www.pacifico.co.jp/eventInfo"},
    {"key": "forum", "name": "東京国際フォーラム", "scope": None,
     "url": "https://www.t-i-forum.co.jp/events/"},
    {"key": "hamamatsu", "name": "浜松商工会議所", "scope": None,
     "url": "https://www.hamamatsu-cci.or.jp/event/"},
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


# ------------------------------------------------------------------- 取得

NOISE = re.compile(
    r"(アクセス|駐車場|お問合せ|お問い合わせ|プライバシー|サイトマップ|"
    r"ご利用案内|よくある|採用|会社概要|ホーム|トップ|一覧へ|詳しく|"
    r"Cookie|ログイン|検索)")
DATE_STR = re.compile(r"[\d０-９]{1,4}\s*[年月/.\-]\s*[\d０-９]{1,2}\s*日?(\s*\([^)]{0,6}\))?")


def get(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r


def harvest(html, base_url, venue, scope, base_year, base_month):
    """日付を含むブロックからイベント候補を拾う汎用パーサ。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    root = soup.select_one(scope) if scope else soup
    if root is None:
        root = soup

    out, seen = [], set()
    for block in root.select("li, tr, article, .event, .eventItem, div"):
        text = " ".join(block.get_text(" ", strip=True).split())
        if not (10 <= len(text) <= 400):
            continue
        start, end = parse_range(text, base_year, base_month)
        if not start:
            continue
        # 入れ子で同じ内容を重複して拾わない
        if any(parse_range(" ".join(c.get_text(" ", strip=True).split()),
                           base_year, base_month)[0]
               for c in block.find_all(["li", "tr", "article"], recursive=True)):
            continue
        link = block.find("a", href=True)
        url = urljoin(base_url, link["href"]) if link else base_url
        title = (link.get_text(" ", strip=True) if link else "").strip()
        if len(title) < 4:
            title = " ".join(DATE_STR.sub(" ", text).split())[:80]
        if not title or NOISE.search(title):
            continue
        key = (title[:40], start.isoformat())
        if key in seen:
            continue
        seen.add(key)
        out.append({"venue": venue, "title": title, "start": start.isoformat(),
                    "end": end.isoformat(), "url": url})
    return out


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
                line = " ".join(line.split())
                if len(line) < 8:
                    continue
                start, end = parse_range(line, y, mo)
                if not start:
                    continue
                title = " ".join(DATE_STR.sub(" ", line).split())[:80]
                if not title or NOISE.search(title):
                    continue
                events.append({"venue": cfg["name"], "title": title,
                               "start": start.isoformat(), "end": end.isoformat(),
                               "url": pdf})
    except Exception as e:
        log.warning("ビッグサイトPDFの取得に失敗: %s", e)
    if not events:
        events = harvest(get(cfg["url"]).text, cfg["url"], cfg["name"],
                         cfg["scope"], y, mo)
    return events


def fetch_all(months, dump_dir=None):
    today = date.today()
    horizon = today + timedelta(days=31 * months)
    events, status = [], []
    for cfg in VENUES:
        try:
            if cfg["key"] == "bigsight":
                ev = fetch_bigsight(cfg, today.year, today.month)
            else:
                res = get(cfg["url"])
                if dump_dir:
                    (dump_dir / f"{cfg['key']}.html").write_text(res.text, encoding="utf-8")
                ev = harvest(res.text, cfg["url"], cfg["name"], cfg["scope"],
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
