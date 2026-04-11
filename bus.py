import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
ETA_URL = "https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/InterCity"
CACHE_FILE = Path(".tdx_token_cache.json")
CONFIG_FILE = Path("config.json")

STOP_STATUS_TEXT = {
    0: "正常",
    1: "尚未發車",
    2: "交管不停靠",
    3: "末班車已過",
    4: "今日未營運",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="查詢 TDX 公路客運路線的預估到站資訊"
    )
    parser.add_argument("--route", default="1815", help="路線名稱，預設 1815")
    parser.add_argument("--stop", help="只顯示站名包含這段文字的站")
    parser.add_argument(
        "--direction",
        type=int,
        choices=(0, 1),
        help="只顯示特定方向，0=去程，1=返程",
    )
    parser.add_argument(
        "--refresh-token",
        action="store_true",
        help="忽略本地 token 快取，強制重新取得 token",
    )
    return parser.parse_args()


def load_config():
    if not CONFIG_FILE.exists():
        return {}

    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"讀取 {CONFIG_FILE} 失敗: {exc}") from exc


def load_cached_token():
    if not CACHE_FILE.exists():
        return None

    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    expires_at = payload.get("expires_at", 0)
    if time.time() >= expires_at:
        return None

    return payload.get("access_token")


def save_cached_token(access_token, expires_in):
    payload = {
        "access_token": access_token,
        # 提前 5 分鐘視為過期，避免邊界時間失效。
        "expires_at": time.time() + max(0, expires_in - 300),
    }
    CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_access_token(force_refresh=False):
    if not force_refresh:
        cached_token = load_cached_token()
        if cached_token:
            return cached_token

    config = load_config()
    client_id = config.get("tdx_client_id") or os.getenv("TDX_CLIENT_ID")
    client_secret = config.get("tdx_client_secret") or os.getenv("TDX_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            "請先在 config.json 或環境變數中設定 TDX_CLIENT_ID 與 TDX_CLIENT_SECRET。"
        )

    payload = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"取得 token 失敗: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"取得 token 失敗: {exc.reason}") from exc

    access_token = body["access_token"]
    expires_in = int(body.get("expires_in", 0))
    save_cached_token(access_token, expires_in)
    return access_token


def fetch_eta(route_name, access_token):
    params = {
        "$filter": f"RouteName/Zh_tw eq '{route_name}'",
        "$orderby": "Direction,StopSequence",
        "$format": "JSON",
    }
    url = f"{ETA_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "authorization": f"Bearer {access_token}",
            "accept": "application/json",
            "accept-encoding": "gzip",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"查詢公車資料失敗: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"查詢公車資料失敗: {exc.reason}") from exc


def format_eta(item):
    estimate_time = item.get("EstimateTime")
    stop_status = item.get("StopStatus")

    if estimate_time is not None:
        if estimate_time <= 60:
            return "進站中"
        minutes = estimate_time // 60
        return f"{minutes} 分"

    return STOP_STATUS_TEXT.get(stop_status, "暫無資料")


def filter_rows(rows, stop_keyword=None, direction=None):
    result = []
    keyword = stop_keyword.casefold() if stop_keyword else None

    for row in rows:
        if direction is not None and row.get("Direction") != direction:
            continue

        stop_name = row.get("StopName", {}).get("Zh_tw", "")
        if keyword and keyword not in stop_name.casefold():
            continue

        result.append(row)

    return result


def print_rows(route_name, rows):
    if not rows:
        print(f"查無路線 {route_name} 的符合資料。")
        print("如果 1815 沒有結果，請再確認是否需要查 1815A、1815B 這類副線。")
        return

    current_direction = None
    for row in rows:
        direction = row.get("Direction")
        if direction != current_direction:
            current_direction = direction
            direction_text = "去程" if direction == 0 else "返程"
            print()
            print(f"[{direction_text}]")

        stop_name = row.get("StopName", {}).get("Zh_tw", "未知站名")
        stop_sequence = row.get("StopSequence", "?")
        eta_text = format_eta(row)
        print(f"{stop_sequence:>2}. {stop_name:<20} {eta_text}")


def main():
    args = parse_args()
    access_token = get_access_token(force_refresh=args.refresh_token)
    rows = fetch_eta(args.route, access_token)
    rows = filter_rows(rows, stop_keyword=args.stop, direction=args.direction)
    print_rows(args.route, rows)


if __name__ == "__main__":
    main()
