# -*- coding: utf-8 -*-
"""
大和川「河合」観測所の水位を、国土交通省「水文水質データベース」から取得し、
決めたしきい値を超えたら ntfy.sh 経由でスマホに通知する。

取得元について（必ずお読みください）:
  取得元の水文水質データベース(www1.river.go.jp)には、「ツール等による自動的な
  データ収集等はサーバに負荷がかかるため、原則としてご遠慮ください」という趣旨の
  注意書きがあります。本プログラムはこれに該当する可能性が高いため、利用にあたっては
  発注者（河川事務所）等に確認のうえで運用してください。
  アクセスは、1回の実行あたり2リクエストに抑えています。

安全のための仕組み:
  ・取得したページに書かれている「観測所記号」と「観測所名」が、設定と一致しない場合は
    通知の判定をせずエラーにします（別の観測所のデータで監視し続けてしまうのを防ぐため）。
  ・取得や通知に失敗した回は終了コード1となり、GitHub Actions上で赤い✕として表示されます。

GitHub Actions で30分おきに自動実行する想定です。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests

# ---- 設定 ----------------------------------------------------------------
# 監視する観測所とそのしきい値(m)。
STATIONS = [
    {
        "id": "306031286607020",  # 観測所記号（水文水質データベース）
        "name": "河合",  # ページ上の観測所名に含まれるべき文字列（照合用）
        "label": "大和川 河合",  # 通知タイトルに使う表示名
        "thresholds": [1.7, 1.8, 1.9, 2.0],
    },
]

# GitHub Secrets で設定する（自分だけが知るランダムな文字列にすること）
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = os.environ.get("STATE_FILE", "data/state.json")

# 自動取得のプログラムであることが分かる名乗りにする
HEADERS = {"User-Agent": "river-level-notifier/1.0 (automated; low frequency)"}
JST = timezone(timedelta(hours=9))


# ---- 取得 ----------------------------------------------------------------
def list_url(station_id: str) -> str:
    return f"https://www1.river.go.jp/cgi-bin/DspWaterData.exe?KIND=9&ID={station_id}"


def http_get(url: str) -> bytes:
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return res.content


def verify_station(html: str, station: dict) -> None:
    """ページに書かれた観測所記号・観測所名が、設定と一致することを確認する。"""
    m = re.search(
        r"<FONT[^>]*>\s*(\d{13,15})\s*</FONT>.*?<FONT[^>]*>([^<]+)</FONT>",
        html,
        re.S | re.I,
    )
    if not m:
        raise RuntimeError(
            "ページから観測所記号・観測所名を読み取れませんでした"
            "（国交省側のページ構造が変わった可能性があります）。"
        )
    page_id, page_name = m.group(1), m.group(2).strip()
    if page_id != station["id"] or station["name"] not in page_name:
        raise RuntimeError(
            f"取得したページの観測所が設定と一致しません。"
            f"設定: {station['id']} / {station['name']}、"
            f"実際のページ: {page_id} / {page_name}"
        )


def find_dat_url(html: str) -> str:
    """一覧ページから、10分水位データ(.dat)のダウンロードリンクを探す。"""
    m = re.search(
        r'href="((?:https?://www1\.river\.go\.jp)?/dat/dload/download/[^"]+\.dat)"',
        html,
    )
    if not m:
        raise RuntimeError("データ(.dat)のリンクが見つかりませんでした。")
    path = m.group(1)
    return path if path.startswith("http") else f"https://www1.river.go.jp{path}"


def parse_latest(text: str):
    """.dat の本文から、最新の有効な (日付, 時刻, 水位m) を返す。"""
    latest = None
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        date_str, time_str, value_str = parts[0], parts[1], parts[2]
        if not re.match(r"^\d{4}/\d{2}/\d{2}$", date_str):
            continue
        try:
            value = float(value_str)
        except ValueError:
            continue  # "-"（未登録）などはスキップ
        if value <= -9000:
            continue  # 欠測はスキップ
        latest = (date_str, time_str, value)
    if latest is None:
        raise RuntimeError("有効な水位データが見つかりませんでした。")
    return latest


def fetch_latest_level(station: dict):
    html = http_get(list_url(station["id"])).decode("euc-jp", errors="ignore")
    verify_station(html, station)
    dat_url = find_dat_url(html)
    text = http_get(dat_url).decode("euc-jp", errors="ignore")
    return parse_latest(text)


# ---- 状態の保存 ------------------------------------------------------------
def load_state() -> dict:
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    state.setdefault("stations", {})
    return state


def save_state(state: dict) -> None:
    folder = os.path.dirname(STATE_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---- 通知 ----------------------------------------------------------------
def send_notification(station: dict, date_str: str, time_str: str, value: float, threshold: float) -> None:
    title = f"{station['label']} 水位アラート"
    message = (
        f"{date_str} {time_str} 時点で水位 {value:.2f}m"
        f"（しきい値 {threshold:.2f}m 以上）になりました。"
    )
    res = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Priority": "high",
            "Tags": "warning,droplet",
        },
        timeout=15,
    )
    res.raise_for_status()  # 送れていないのに「通知済み」にしないため


# ---- 1観測所分の処理 ---------------------------------------------------------
def process_station(station: dict, st: dict) -> None:
    date_str, time_str, value = fetch_latest_level(station)
    print(
        f"[INFO] {station['label']}: 最新観測値 {date_str} {time_str} = {value:.2f}m "
        f"(前回チェック時: {st.get('last_level')})"
    )

    notified = set(st.get("notified_thresholds", []))
    for th in sorted(station["thresholds"]):
        if value >= th:
            if th in notified:
                print(f"[INFO]   {th:.2f}m: 既に通知済みのため送信しません。")
                continue
            send_notification(station, date_str, time_str, value, th)
            notified.add(th)
            st["notified_thresholds"] = sorted(notified)
            print(f"[INFO]   {th:.2f}m 以上のため通知を送信しました。")
        elif th in notified:
            notified.discard(th)
            st["notified_thresholds"] = sorted(notified)
            print(f"[INFO]   水位が {th:.2f}m 未満に戻ったため、通知フラグをリセットします。")

    st["notified_thresholds"] = sorted(notified)
    st["last_level"] = value
    st["last_observed"] = f"{date_str} {time_str}"


def main() -> None:
    state = load_state()
    failures = []

    for station in STATIONS:
        st = state["stations"].setdefault(station["id"], {"notified_thresholds": []})
        try:
            process_station(station, st)
            st["last_error"] = None
        except Exception as e:  # 1か所の失敗で、もう1か所の監視を止めない
            msg = f"{type(e).__name__}: {e}"
            print(f"[ERROR] {station['label']}: {msg}", file=sys.stderr)
            st["last_error"] = msg
            failures.append(station["label"])

    state["last_checked"] = datetime.now(JST).isoformat()
    save_state(state)

    if failures:
        print(f"[ERROR] 取得に失敗した観測所: {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
