import argparse
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# API endpoints and local file paths.
# API 端點與本地檔案路徑設定。
TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
ETA_URL = "https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/InterCity"
CACHE_FILE = Path(".tdx_token_cache.json")
CONFIG_FILE = Path("config.json")
DEBUG_LOG_FILE = Path("tdx_debug_log.json")

# Human-readable stop status mapping from TDX codes.
# 把 TDX 的站牌狀態代碼轉成人看得懂的文字。
STOP_STATUS_TEXT = {
    0: "正常",
    1: "尚未發車",
    2: "交管不停靠",
    3: "末班車已過",
    4: "今日未營運",
}


def parse_args():
    # Command-line options for route, filtering, and debug behavior.
    # 終端機參數設定，控制路線、篩選條件與除錯模式。
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
    parser.add_argument(
        "--debug",
        action="store_true",
        help="印出 request / response 除錯資訊",
    )
    return parser.parse_args()


def load_config():
    # Read local config from config.json.
    # 從 config.json 讀取本地設定。
    if not CONFIG_FILE.exists():
        return {}

    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"讀取 {CONFIG_FILE} 失敗: {exc}") from exc


def mask_secret(value, keep=4):
    # Hide most of a secret before printing it to logs.
    # 輸出到 log 前先遮住大部分敏感字串。
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep)}"


def debug_print(title, payload):
    # Print debug data to the terminal in a readable format.
    # 以較易讀的格式把除錯資訊印到終端機。
    print()
    print(f"=== {title} ===")
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload)


def iso_now():
    # Generate an ISO timestamp for debug records.
    # 產生 ISO 格式時間字串，方便記錄 debug 時間點。
    return datetime.now(timezone.utc).astimezone().isoformat()


def load_debug_log():
    # Load the existing debug log file, or start with an empty list.
    # 讀取既有 debug log；如果沒有就從空清單開始。
    if not DEBUG_LOG_FILE.exists():
        return []

    try:
        payload = json.loads(DEBUG_LOG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    return payload if isinstance(payload, list) else []


def append_debug_log(title, payload):
    # Append one debug event to the shared JSON log file.
    # 把單次 debug 事件追加到同一份 JSON log 檔。
    logs = load_debug_log()
    logs.append(
        {
            "timestamp": iso_now(),
            "title": title,
            "payload": payload,
        }
    )
    DEBUG_LOG_FILE.write_text(
        json.dumps(logs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record_debug(title, payload, enabled=False, log_payload=None):
    # Only record debug output when --debug is enabled.
    # 只有在開啟 --debug 時才輸出並寫入除錯資訊。
    if not enabled:
        return
    debug_print(title, payload)
    append_debug_log(title, log_payload if log_payload is not None else payload)


def read_json_response(response):
    # TDX may return gzip-compressed JSON, so decode in two steps:
    # first decompress if needed, then parse JSON text.
    # TDX 可能回傳 gzip 壓縮的 JSON，因此要先視情況解壓，再解析 JSON。
    raw_body = response.read()
    content_encoding = response.headers.get("Content-Encoding", "").lower()
    if content_encoding == "gzip":
        raw_body = gzip.decompress(raw_body)
    return json.loads(raw_body.decode("utf-8"))


def load_cached_token():
    # Try to reuse a local token if it exists and is not expired yet.
    # 如果本地 token 還沒過期，就直接重用，避免每次都重新取 token。
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
    # Save the token locally and treat it as expired 5 minutes early.
    # 把 token 存到本地，並提前 5 分鐘視為過期，避免壓線失效。
    payload = {
        "access_token": access_token,
        # 提前 5 分鐘視為過期，避免邊界時間失效。
        "expires_at": time.time() + max(0, expires_in - 300),
    }
    CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_access_token(force_refresh=False, debug=False):
    # Authentication flow:
    # 1. try cached token
    # 2. if needed, POST client credentials to TDX token endpoint
    # 3. return access token for later API calls
    # 認證流程：
    # 1. 先試本地快取
    # 2. 必要時用 client credentials POST 到 token endpoint
    # 3. 取得後回傳 access token 給後續 API 使用
    if not force_refresh:
        cached_token = load_cached_token()
        if cached_token:
            record_debug(
                "TOKEN RESPONSE (CACHE)",
                {"access_token": mask_secret(cached_token), "source": "cache"},
                enabled=debug,
            )
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

    record_debug(
        "TOKEN REQUEST",
        {
            "method": "POST",
            "url": TOKEN_URL,
            "headers": {"content-type": "application/x-www-form-urlencoded"},
            "body": {
                "grant_type": "client_credentials",
                "client_id": mask_secret(client_id),
                "client_secret": mask_secret(client_secret),
            },
        },
        enabled=debug,
    )

    request = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = read_json_response(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"取得 token 失敗: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"取得 token 失敗: {exc.reason}") from exc

    access_token = body["access_token"]
    expires_in = int(body.get("expires_in", 0))
    record_debug(
        "TOKEN RESPONSE",
        {
            "access_token": mask_secret(access_token),
            "expires_in": expires_in,
            "token_type": body.get("token_type"),
        },
        enabled=debug,
    )
    save_cached_token(access_token, expires_in)
    return access_token


def fetch_eta(route_name, access_token, debug=False):
    # Build a RESTful GET request for InterCity ETA data.
    # 組出查詢公路客運 ETA 的 RESTful GET 請求。
    params = {
        "$filter": f"RouteName/Zh_tw eq '{route_name}'",
        "$orderby": "Direction,StopSequence",
        "$format": "JSON",
    }
    url = f"{ETA_URL}?{urllib.parse.urlencode(params)}"
    headers = {
        "authorization": f"Bearer {access_token}",
        "accept": "application/json",
        "accept-encoding": "gzip",
    }
    record_debug(
        "ETA REQUEST",
        {
            "method": "GET",
            "url": url,
            "headers": {
                "authorization": f"Bearer {mask_secret(access_token)}",
                "accept": "application/json",
                "accept-encoding": "gzip",
            },
        },
        enabled=debug,
    )
    request = urllib.request.Request(
        url,
        headers=headers,
    )

    try:
        # The token is sent in Authorization: Bearer ...
        # token 會放在 Authorization: Bearer ... header 裡。
        with urllib.request.urlopen(request, timeout=20) as response:
            body = read_json_response(response)
            record_debug(
                "ETA RESPONSE",
                {
                    "count": len(body),
                    "sample": body[:3],
                },
                enabled=debug,
                log_payload={
                    "count": len(body),
                    "records": body,
                },
            )
            return body
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"查詢公車資料失敗: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"查詢公車資料失敗: {exc.reason}") from exc


def format_eta(item):
    # Convert raw ETA fields into short display text.
    # 把原始 ETA 欄位整理成適合顯示的短文字。
    estimate_time = item.get("EstimateTime")
    stop_status = item.get("StopStatus")

    if estimate_time is not None:
        if estimate_time <= 60:
            return "進站中"
        minutes = estimate_time // 60
        return f"{minutes} 分"

    return STOP_STATUS_TEXT.get(stop_status, "暫無資料")


def direction_text(direction):
    return "去程" if direction == 0 else "返程"


def plate_text(item):
    plate = item.get("PlateNumb")
    if not plate or plate == "-1":
        return "未提供"
    return plate


def subroute_text(item):
    return item.get("SubRouteName", {}).get("Zh_tw") or item.get("RouteName", {}).get(
        "Zh_tw", "未知路線"
    )


def update_time_text(item):
    update_time = item.get("UpdateTime") or item.get("DataTime")
    if not update_time:
        return "未知"
    return update_time


def filter_rows(rows, stop_keyword=None, direction=None):
    # Narrow the API result set by stop name keyword and direction.
    # 依站名關鍵字與方向，縮小 API 查詢結果。
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


def print_rows(route_name, rows, stop_keyword=None):
    # Print the final result in a terminal-friendly format.
    # 以適合終端機閱讀的方式印出最終結果。
    if not rows:
        print(f"查無路線 {route_name} 的符合資料。")
        print("如果 1815 沒有結果，請再確認是否需要查 1815A、1815B 這類副線。")
        return

    if stop_keyword:
        rows = sorted(
            rows,
            key=lambda row: (
                row.get("Direction", 99),
                row.get("EstimateTime") is None,
                row.get("EstimateTime", 10**9),
                row.get("SubRouteName", {}).get("Zh_tw", ""),
                row.get("PlateNumb", ""),
            ),
        )
        current_direction = None
        for row in rows:
            direction = row.get("Direction")
            if direction != current_direction:
                current_direction = direction
                print()
                print(f"[{direction_text(direction)}]")

            stop_name = row.get("StopName", {}).get("Zh_tw", "未知站名")
            eta_text = format_eta(row)
            subroute_name = subroute_text(row)
            plate_number = plate_text(row)
            stop_sequence = row.get("StopSequence", "?")
            update_text = update_time_text(row)
            print(
                f"{subroute_name} | 站牌: {stop_name} | 車牌: {plate_number} | "
                f"到站: {eta_text} | 站序: {stop_sequence} | 更新: {update_text}"
            )
        return

    current_direction = None
    for row in rows:
        direction = row.get("Direction")
        if direction != current_direction:
            current_direction = direction
            print()
            print(f"[{direction_text(direction)}]")

        stop_name = row.get("StopName", {}).get("Zh_tw", "未知站名")
        stop_sequence = row.get("StopSequence", "?")
        eta_text = format_eta(row)
        subroute_name = subroute_text(row)
        plate_number = plate_text(row)
        print(
            f"{stop_sequence:>2}. {stop_name:<20} {eta_text:<8} "
            f"子路線: {subroute_name:<8} 車牌: {plate_number}"
        )


def main():
    # Main flow:
    # parse args -> get token -> query ETA -> filter -> print
    # 主流程：
    # 讀參數 -> 取 token -> 查 ETA -> 篩選 -> 輸出
    args = parse_args()
    access_token = get_access_token(
        force_refresh=args.refresh_token,
        debug=args.debug,
    )
    rows = fetch_eta(args.route, access_token, debug=args.debug)
    rows = filter_rows(rows, stop_keyword=args.stop, direction=args.direction)
    print_rows(args.route, rows, stop_keyword=args.stop)


if __name__ == "__main__":
    main()
