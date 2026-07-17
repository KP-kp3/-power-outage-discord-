#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
各電力会社（大手10社）の停電情報を巡回し、状態が変化したときだけ
Discord Webhook に通知するスクリプト。

- 状態遷移（停電なし→発生 / 発生→復旧 / 戸数の大きな変化）だけ通知するので、
  5分ごとに実行してもスパムにならない（エッジトリガ）。
- HTMLに直接データがある会社は requests で取得。
- JavaScriptで描画する会社は Playwright（ヘッドレスChromium）で描画してから取得。

環境変数:
  DISCORD_WEBHOOK_URL : 必須。Discordのウェブフック URL。
  STATE_FILE          : 任意。状態保存ファイル（既定 state.json）。
  NOTIFY_RESTORE      : 任意。"0" で復旧通知をオフ（既定オン）。
"""

import os
import re
import sys
import json
import time
import datetime
import traceback

import requests
from bs4 import BeautifulSoup

JST = datetime.timezone(datetime.timedelta(hours=9))
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
NOTIFY_RESTORE = os.environ.get("NOTIFY_RESTORE", "1") != "0"
# 戸数がこの割合以上変化したら「拡大/縮小」を通知（0.5 = 50%）
CHANGE_RATIO = 0.5
# 戸数がこの絶対値以上変化したら通知
CHANGE_ABS = 1000

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------------------
# 取得ユーティリティ
# ---------------------------------------------------------------------------
def fetch_http(url, timeout=25):
    """requests でHTMLを取得し、可視テキストを返す。"""
    r = requests.get(url, headers={"User-Agent": UA,
                                   "Accept-Language": "ja,en;q=0.8"},
                     timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


_PW = {"pw": None, "browser": None}


def _get_browser():
    """Playwright のブラウザを使い回す。"""
    if _PW["browser"] is None:
        from playwright.sync_api import sync_playwright
        _PW["pw"] = sync_playwright().start()
        _PW["browser"] = _PW["pw"].chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"])
    return _PW["browser"]


def fetch_browser(url, wait_ms=6000, timeout=45000):
    """Playwright で描画後の可視テキストを返す。"""
    browser = _get_browser()
    ctx = browser.new_context(user_agent=UA, locale="ja-JP")
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout)
    except Exception:
        # networkidle にならないサイトもあるので domcontentloaded で再試行
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            pass
    page.wait_for_timeout(wait_ms)
    text = page.evaluate("() => document.body ? document.body.innerText : ''")
    ctx.close()
    return text or ""


def close_browser():
    if _PW["browser"] is not None:
        try:
            _PW["browser"].close()
            _PW["pw"].stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 検知ヘルパー
# ---------------------------------------------------------------------------
def _to_int(s):
    try:
        return int(str(s).replace(",", "").replace("，", "").strip())
    except Exception:
        return None


def households_from(text, pattern):
    """pattern（1グループ=数値）で戸数を取り出す。無ければ None。"""
    if not pattern:
        return None
    m = re.search(pattern, text)
    if m:
        return _to_int(m.group(1))
    return None


# 各社の検知関数は (has_outage: bool|None, households: int|None, summary: str) を返す。
# has_outage が None のときは「判定不能」で通知しない（誤検知でのスパムを避ける）。

def detect_no_outage_phrase(text, phrase, total_pattern=None):
    """『停電はありません』系フレーズの有無で判定する共通ロジック。"""
    hh = households_from(text, total_pattern)
    if phrase in text:
        return (False, hh if hh is not None else 0, "停電なし")
    # フレーズが無い＝発生の可能性。戸数が取れればそれを使う。
    if hh is not None:
        return (hh > 0, hh, f"停電発生中（約{hh:,}戸）" if hh > 0 else "停電なし")
    return (True, None, "停電が発生している可能性があります")


def detect_hokkaido(text):
    return detect_no_outage_phrase(
        text, "現在、停電は発生しておりません",
        total_pattern=r"全道計[^\d]*([\d,]+)\s*戸")


def detect_hokuriku(text):
    return detect_no_outage_phrase(text, "現在、停電は発生しておりません")


def detect_kansai(text):
    return detect_no_outage_phrase(text, "現在停電情報はございません")


def detect_shikoku(text):
    # 4県それぞれ「停電情報はありません」と出る。全部揃えば停電なし。
    n = text.count("停電情報はありません")
    prefs = []
    for pref in ["香川県", "愛媛県", "徳島県", "高知県"]:
        # 「香川県 停電情報はありません」でなければ発生とみなす
        if re.search(pref + r"\s*停電情報はありません", text) is None and pref in text:
            prefs.append(pref)
    if n >= 4 and not prefs:
        return (False, 0, "停電なし")
    if prefs:
        return (True, None, "停電発生中: " + "・".join(prefs))
    # 判定材料が揃わない
    return (None, None, "判定不能")


def detect_chugoku(text):
    # 「県 約 N 戸」形式（凡例には出ない）を停電の根拠にする。
    matches = re.findall(r"([一-龥]{1,3}[県府都])\s*約\s*([\d,]+)\s*戸", text)
    if matches:
        total = sum(_to_int(m[1]) or 0 for m in matches)
        parts = [f"{m[0]}約{_to_int(m[1]):,}戸" for m in matches]
        return (True, total, "停電発生中: " + " / ".join(parts))
    # 発生中エリアの見出しはあるが件数が取れない場合
    if "現在発生中の停電" in text and "約" in text:
        return (True, None, "停電発生中")
    return (False, 0, "停電なし")


# --- JavaScript描画（Playwright）会社: ベストエフォート -----------------
# 実データ（実際に停電が起きている状態）で表示文言を確認できていないため、
# 「発生を示す語 or 県＋戸数」があるときだけ発生と判定し、無ければ停電なし扱い。
# → 取りこぼしても、誤通知でスパムするよりは安全側に倒す方針。

_PREF_HH = r"([一-龥]{1,3}[県府都])\s*(?:約)?\s*([\d,]{2,})\s*戸"
_NO_OUTAGE_WORDS = [
    "停電は発生しておりません", "停電は発生していません",
    "停電情報はありません", "現在、停電はありません",
    "停電情報はございません", "現在発生している停電はありません",
]


def detect_generic_js(text):
    if any(w in text for w in _NO_OUTAGE_WORDS):
        return (False, 0, "停電なし")
    matches = re.findall(_PREF_HH, text)
    # 凡例の「1,000戸以上」等を除くため、県名が前置されたものだけ採用
    real = [(m[0], _to_int(m[1])) for m in matches if _to_int(m[1])]
    if real:
        total = sum(h for _, h in real if h)
        parts = [f"{p}約{h:,}戸" for p, h in real][:8]
        return (True, total, "停電発生中: " + " / ".join(parts))
    # 発生を示す語
    if re.search(r"停電(が発生|発生中|中の停電)", text):
        return (True, None, "停電発生の可能性")
    return (False, 0, "停電なし")


# ---------------------------------------------------------------------------
# 会社定義
# ---------------------------------------------------------------------------
PROVIDERS = [
    # key, 表示名, 取得方法, URL, 検知関数, 信頼度
    ("hokkaido", "北海道電力ネットワーク", "http",
     "https://teiden-info.hepco.co.jp/", detect_hokkaido, "high"),
    ("tohoku", "東北電力ネットワーク", "browser",
     "https://nw.tohoku-epco.co.jp/teideninfo/", detect_generic_js, "best-effort"),
    ("tepco", "東京電力パワーグリッド", "browser",
     "https://teideninfo.tepco.co.jp/", detect_generic_js, "best-effort"),
    ("chubu", "中部電力パワーグリッド", "browser",
     "https://teiden.chuden.jp/p/index.html", detect_generic_js, "best-effort"),
    ("hokuriku", "北陸電力送配電", "http",
     "https://www.rikuden.co.jp/nw/teiden/otj010.html", detect_hokuriku, "high"),
    ("kansai", "関西電力送配電", "http",
     "https://www.kansai-td.co.jp/teiden-info/index.php", detect_kansai, "high"),
    ("chugoku", "中国電力ネットワーク", "http",
     "https://www.teideninfo.energia.co.jp/", detect_chugoku, "high"),
    ("shikoku", "四国電力送配電", "http",
     "https://www.yonden.co.jp/nw/teiden-info/index.html", detect_shikoku, "high"),
    ("kyushu", "九州電力送配電", "browser",
     "https://www.kyuden.co.jp/td_teiden/kyushu.html", detect_generic_js, "best-effort"),
    ("okinawa", "沖縄電力", "browser",
     "https://www.okidenmail.jp/bosai/info/index.html", detect_generic_js, "best-effort"),
]


# ---------------------------------------------------------------------------
# 状態管理・通知
# ---------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def post_discord(embeds):
    if not WEBHOOK:
        print("[WARN] DISCORD_WEBHOOK_URL 未設定。通知をスキップします。", file=sys.stderr)
        return
    # Discord は 1メッセージ最大10 embed
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        payload = {"username": "停電速報", "embeds": chunk}
        for attempt in range(3):
            try:
                res = requests.post(WEBHOOK, json=payload, timeout=20)
                if res.status_code == 429:  # レート制限
                    wait = res.json().get("retry_after", 2)
                    time.sleep(float(wait) + 0.5)
                    continue
                res.raise_for_status()
                break
            except Exception as e:
                print(f"[WARN] Discord投稿失敗({attempt+1}/3): {e}", file=sys.stderr)
                time.sleep(2)


def make_embed(company, url, kind, summary, households):
    colors = {"new": 0xE74C3C, "worse": 0xE67E22,
              "better": 0xF1C40F, "restore": 0x2ECC71}
    titles = {"new": "⚡ 新規停電", "worse": "🔺 停電拡大",
              "better": "🔻 停電縮小", "restore": "✅ 復旧"}
    now = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    desc = summary or ""
    if households:
        desc += f"\n**停電戸数: 約{households:,}戸**"
    desc += f"\n[公式の停電情報ページを開く]({url})"
    return {
        "title": f"{titles[kind]}｜{company}",
        "description": desc,
        "color": colors[kind],
        "footer": {"text": f"{now} JST 時点"},
    }


def decide_events(prev, cur):
    """前回状態 prev と今回 cur から通知イベント種別を決める。"""
    events = []
    p_out = prev.get("has_outage") if prev else False
    c_out = cur["has_outage"]

    if c_out is None:          # 判定不能 → 何もしない（前回状態は保持）
        return events, prev if prev else cur

    if not p_out and c_out:
        events.append("new")
    elif p_out and not c_out:
        if NOTIFY_RESTORE:
            events.append("restore")
    elif p_out and c_out:
        ph = prev.get("households")
        ch = cur.get("households")
        if ph and ch:
            diff = ch - ph
            if abs(diff) >= CHANGE_ABS and abs(diff) >= ph * CHANGE_RATIO:
                events.append("worse" if diff > 0 else "better")
    return events, cur


def run_once():
    state = load_state()
    new_state = dict(state)
    embeds = []

    for key, name, mode, url, detector, conf in PROVIDERS:
        try:
            text = fetch_browser(url) if mode == "browser" else fetch_http(url)
            has_outage, households, summary = detector(text)
            cur = {
                "has_outage": has_outage,
                "households": households,
                "summary": summary,
                "checked_at": datetime.datetime.now(JST).isoformat(),
                "error": None,
            }
            print(f"[OK] {name}: outage={has_outage} hh={households} ({summary})")
        except Exception as e:
            # 取得失敗は前回状態を維持し、フラグを立てない（誤通知防止）
            print(f"[ERR] {name}: {e}", file=sys.stderr)
            traceback.print_exc()
            prev = state.get(key, {})
            cur = dict(prev)
            cur["error"] = str(e)
            cur["checked_at"] = datetime.datetime.now(JST).isoformat()
            new_state[key] = cur
            continue

        events, merged = decide_events(state.get(key), cur)
        for kind in events:
            embeds.append(make_embed(name, url, kind,
                                     cur["summary"], cur["households"]))
        # 判定不能時は前回状態を保持
        new_state[key] = merged if cur["has_outage"] is not None else state.get(key, cur)

    close_browser()

    if embeds:
        post_discord(embeds)
        print(f"[INFO] {len(embeds)}件を通知しました。")
    else:
        print("[INFO] 状態変化なし。通知はありません。")

    save_state(new_state)
    return 0


if __name__ == "__main__":
    sys.exit(run_once())
