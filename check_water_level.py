# -*- coding: utf-8 -*-
"""
大和川・藤井観測所の水位を、国土交通省「水文水質データベース」から取得し、
指定したしきい値（既定 0.9m）以上になったら ntfy.sh 経由でスマホに通知する。

データ取得元について:
  「川の防災情報」(river.go.jp/kawabou) のトップページは
  「ツール等による定期的なデータ収集はお控えください」と明記しているため、
  このスクリプトはそちらには一切アクセスしません。
  代わりに、数値データの参照用として公開されている
  「水文水質データベース」(www1.river.go.jp) のリアルタイム10分水位ページを
  30分に1回程度の低頻度で参照します。

GitHub Actions で30分おきに自動実行する想定です。
"""

import os
import re
import json
from datetime import datetime, timezone, timedelta

import requests

# ---- 設定 ----
STATION_ID = "306031286607020"  # 大和川水系 大和川 河合（かわい）観測所

# しきい値は複数指定できる。カンマ区切りで環境変数 THRESHOLDS に渡す（例: "0.9,1.0,1.1"）
THRESHOLDS = sorted(
    float(x) for x in os.environ.get("THRESHOLDS", "0.9").split(",") if x.strip()
)

NTFY_TOPIC = os.environ["NTFY_TOPIC"]  # GitHub Secrets で設定する（自分だけが知るランダムな文字列にすること）
STATE_FILE = "data/state.json"

SITE_INFO_URL = f"https://www1.river.go.jp/cgi-bin/SiteInfo.exe?ID={STATION_ID}"
REALTIME_LIST_URL = (
    f"https://www1.river.go.jp/cgi-bin/DspWaterData.exe?KIND=9&ID={STATION_ID}"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
}


def fetch_dat_url() -> str:
    """観測所のリアルタイム10分水位一覧ページから、最新の .dat ダウンロードリンクを取得する。

    国交省側は .dat のファイル名に毎回変わるPIDのような値を含めているため、
    URLを直接組み立てず、必ずこの一覧ページからリンクを抽出する。
    通常のブラウザ操作を模して、観測所情報ページ→リアルタイム一覧ページの順にアクセスする。
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # まず観測所情報ページを踏んでからリアルタイムページへ（通常のブラウジング動線を模す）
    session.get(SITE_INFO_URL, timeout=30)

    res = session.get(
        REALTIME_LIST_URL, headers={"Referer": SITE_INFO_URL}, timeout=30
    )
    res.raise_for_status()
    html = res.content.decode("euc-jp", errors="ignore")

    # dat へのリンクは絶対URL(http://www1.river.go.jp/...)の場合と
    # 相対パス(/dat/dload/...)の場合があるため、両方に対応する。
    m = re.search(
        r'href="((?:https?://www1\.river\.go\.jp)?/dat/dload/download/[^"]+\.dat)"',
        html,
    )
    if not m:
        # デバッグ用に、取得できたページの状態をログに出す
        print(f"[DEBUG] status_code={res.status_code}")
        print(f"[DEBUG] final url={res.url}")
        print(f"[DEBUG] content length={len(html)}")
        print(f"[DEBUG] head of content:\n{html[:1500]}")
        raise RuntimeError(
            "dat リンクが見つかりませんでした。国交省側のページ構造が変わったか、"
            "アクセス元が制限されている可能性があります。上の[DEBUG]ログを確認してください。"
        )
    dat_path = m.group(1)
    if dat_path.startswith("http"):
        return dat_path
    return f"https://www1.river.go.jp{dat_path}"


def fetch_latest_level(dat_url: str):
    """.dat をダウンロードし、最新の有効な (日付, 時刻, 水位m) を返す。"""
    res = requests.get(dat_url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    text = res.content.decode("euc-jp", errors="ignore")

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
            # "-"（未登録）などはスキップ
            continue
        if value <= -9000:
            # -9999.99 (欠測) はスキップ
            continue
        latest = (date_str, time_str, value)

    if latest is None:
        raise RuntimeError("有効な水位データが見つかりませんでした。")
    return latest


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            state.setdefault("notified_thresholds", [])
            return state
    return {"notified_thresholds": [], "last_checked": None, "last_level": None}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_notification(date_str: str, time_str: str, value: float, threshold: float) -> None:
    title = "大和川 河合 水位アラート"
    message = (
        f"{date_str} {time_str} 時点で水位 {value:.2f}m"
        f"（しきい値 {threshold:.2f}m 以上）になりました。"
    )
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Priority": "high",
            "Tags": "warning,droplet",
        },
        timeout=15,
    )


def main() -> None:
    dat_url = fetch_dat_url()
    date_str, time_str, value = fetch_latest_level(dat_url)
    state = load_state()
    notified = set(state.get("notified_thresholds", []))

    print(
        f"[INFO] 最新観測値: {date_str} {time_str} = {value:.2f}m "
        f"(前回チェック時の値: {state.get('last_level')})"
    )
    print(f"[INFO] しきい値: {THRESHOLDS}")

    for th in THRESHOLDS:
        if value >= th:
            if th not in notified:
                send_notification(date_str, time_str, value, th)
                notified.add(th)
                print(f"[INFO] しきい値 {th:.2f}m 以上のため通知を送信しました。")
            else:
                print(f"[INFO] しきい値 {th:.2f}m は既に通知済みのため送信しません。")
        else:
            if th in notified:
                notified.discard(th)
                print(f"[INFO] 水位がしきい値 {th:.2f}m 未満に戻ったため、通知フラグをリセットします。")

    state["notified_thresholds"] = sorted(notified)

    jst = timezone(timedelta(hours=9))
    state["last_checked"] = datetime.now(jst).isoformat()
    state["last_level"] = value
    save_state(state)


if __name__ == "__main__":
    main()
