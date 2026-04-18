import argparse
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import sys
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

MAIN_ROUTE_OPTIONS = ["1813", "1815"]


def parse_args():
    # Command-line options for route, filtering, and debug behavior.
    # 終端機參數設定，控制路線、篩選條件與除錯模式。
    parser = argparse.ArgumentParser(
        description="查詢 TDX 公路客運路線的預估到站資訊"
    )
    parser.add_argument("--route", help="路線名稱，例如 1815")
    parser.add_argument("--subroute", help="子路線名稱，例如 1815A")
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
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="使用互動式選單選擇主路線、子路線、方向與站點",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="開啟圖形化視窗模式",
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


def get_stop_name(row):
    return row.get("StopName", {}).get("Zh_tw", "未知站名")


def filter_by_subroute(rows, subroute_name=None):
    if not subroute_name:
        return rows

    result = []
    for row in rows:
        if subroute_text(row) == subroute_name:
            result.append(row)
    return result


def build_direction_options(rows):
    options = []
    for direction in sorted({row.get("Direction") for row in rows}):
        direction_rows = [row for row in rows if row.get("Direction") == direction]
        if not direction_rows:
            continue

        ordered_rows = sorted(
            direction_rows,
            key=lambda row: row.get("StopSequence", 10**9),
        )
        destination_name = get_stop_name(ordered_rows[-1])
        options.append(
            {
                "value": direction,
                "label": f"{direction_text(direction)} - 往 {destination_name}",
            }
        )
    return options


def build_stop_options(rows):
    options = []
    seen = set()
    for row in sorted(rows, key=lambda item: item.get("StopSequence", 10**9)):
        stop_name = get_stop_name(row)
        if stop_name in seen:
            continue
        seen.add(stop_name)
        options.append(stop_name)
    return options


def get_subroutes(rows):
    return sorted({subroute_text(row) for row in rows})


def get_rows_for_route(access_token, route_name, debug=False):
    return fetch_eta(route_name, access_token, debug=debug)


def get_rows_for_subroute(rows, subroute_name):
    return filter_by_subroute(rows, subroute_name)


def get_rows_for_direction(rows, direction):
    return filter_rows(rows, direction=direction)


def get_rows_for_stop(rows, stop_name):
    if not stop_name:
        return rows
    return [row for row in rows if get_stop_name(row) == stop_name]


def format_row_detail(row):
    return (
        f"{subroute_text(row)} | 站牌: {get_stop_name(row)} | 車牌: {plate_text(row)} | "
        f"到站: {format_eta(row)} | 站序: {row.get('StopSequence', '?')} | "
        f"更新: {update_time_text(row)}"
    )


def build_result_lines(route_name, rows, stop_keyword=None):
    if not rows:
        return [
            f"查無路線 {route_name} 的符合資料。",
            "如果查不到，請再確認主路線、子路線、方向或站點是否正確。",
        ]

    lines = []
    if stop_keyword:
        ordered_rows = sorted(
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
        for row in ordered_rows:
            direction = row.get("Direction")
            if direction != current_direction:
                current_direction = direction
                lines.append(f"[{direction_text(direction)}]")
            lines.append(format_row_detail(row))
        return lines

    current_direction = None
    for row in rows:
        direction = row.get("Direction")
        if direction != current_direction:
            current_direction = direction
            lines.append(f"[{direction_text(direction)}]")
        lines.append(
            f"{row.get('StopSequence', '?'):>2}. {get_stop_name(row):<20} "
            f"{format_eta(row):<8} 子路線: {subroute_text(row):<8} 車牌: {plate_text(row)}"
        )

    return lines


def choose_from_menu(title, options, allow_all=False, all_label="全部"):
    if not options:
        raise RuntimeError(f"{title} 沒有可選項目。")

    while True:
        print()
        print(title)
        if allow_all:
            print(f"0. {all_label}")
        for index, option in enumerate(options, start=1):
            label = option["label"] if isinstance(option, dict) else str(option)
            print(f"{index}. {label}")

        raw = input("請輸入編號: ").strip()
        if allow_all and raw == "0":
            return None
        if not raw.isdigit():
            print("請輸入數字編號。")
            continue

        selected_index = int(raw) - 1
        if 0 <= selected_index < len(options):
            selected = options[selected_index]
            return selected["value"] if isinstance(selected, dict) else selected

        print("輸入超出範圍，請重新選擇。")


def run_interactive_selection(access_token, debug=False):
    route_name = choose_from_menu("請選擇主路線", MAIN_ROUTE_OPTIONS)
    rows = get_rows_for_route(access_token, route_name, debug=debug)

    subroutes = get_subroutes(rows)
    subroute_name = choose_from_menu("請選擇子路線", subroutes)
    rows = get_rows_for_subroute(rows, subroute_name)

    direction_options = build_direction_options(rows)
    direction = choose_from_menu("請選擇方向", direction_options)
    rows = get_rows_for_direction(rows, direction)

    stop_options = build_stop_options(rows)
    stop_name = choose_from_menu(
        "請選擇站點",
        stop_options,
        allow_all=True,
        all_label="全部站點",
    )
    rows = get_rows_for_stop(rows, stop_name)

    print_rows(route_name, rows, stop_keyword=stop_name)


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


def run_gui(access_token, debug=False):
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as exc:
        raise RuntimeError("目前環境無法使用 tkinter，無法開啟 GUI 視窗。") from exc

    route_rows_cache = {}

    def get_route_rows(route_name):
        if route_name not in route_rows_cache:
            route_rows_cache[route_name] = get_rows_for_route(
                access_token,
                route_name,
                debug=debug,
            )
        return route_rows_cache[route_name]

    root = tk.Tk()
    root.title("Bus Board")
    root.geometry("1180x820")
    root.configure(bg="#f1efe7")

    palette = {
        "bg": "#f1efe7",
        "panel": "#fffdf7",
        "text": "#1d2a31",
        "muted": "#5f6b71",
        "accent": "#0f766e",
        "accent_dark": "#0b5f59",
        "line": "#ded8cc",
        "soon": "#f59e0b",
        "arriving": "#ef4444",
        "normal": "#2563eb",
        "done": "#94a3b8",
    }

    state = {
        "route": None,
        "subroute": None,
        "direction": None,
        "direction_label": None,
        "stop": None,
    }

    outer = tk.Frame(root, bg=palette["bg"], padx=28, pady=24)
    outer.pack(fill="both", expand=True)

    title = tk.Label(
        outer,
        text="台灣客運看板",
        bg=palette["bg"],
        fg=palette["text"],
        font=("Helvetica", 26, "bold"),
    )
    title.pack(anchor="w")

    subtitle = tk.Label(
        outer,
        text="依序選主路線、子路線、方向與站點。",
        bg=palette["bg"],
        fg=palette["muted"],
        font=("Helvetica", 11),
    )
    subtitle.pack(anchor="w", pady=(6, 16))

    breadcrumb_var = tk.StringVar(value="尚未開始選擇")
    breadcrumb = tk.Label(
        outer,
        textvariable=breadcrumb_var,
        bg=palette["bg"],
        fg=palette["accent_dark"],
        font=("Helvetica", 11, "bold"),
    )
    breadcrumb.pack(anchor="w", pady=(0, 12))

    step_card = tk.Frame(
        outer,
        bg=palette["panel"],
        highlightbackground=palette["line"],
        highlightthickness=1,
        padx=18,
        pady=18,
    )
    step_card.pack(fill="x")

    step_title_var = tk.StringVar(value="1. 選擇主路線")
    step_help_var = tk.StringVar(value="先選 1813 或 1815。")

    step_title = tk.Label(
        step_card,
        textvariable=step_title_var,
        bg=palette["panel"],
        fg=palette["text"],
        font=("Helvetica", 18, "bold"),
    )
    step_title.pack(anchor="w")

    step_help = tk.Label(
        step_card,
        textvariable=step_help_var,
        bg=palette["panel"],
        fg=palette["muted"],
        font=("Helvetica", 10),
    )
    step_help.pack(anchor="w", pady=(6, 14))

    button_wrap = tk.Frame(step_card, bg=palette["panel"])
    button_wrap.pack(fill="both", expand=True)
    button_wrap.grid_rowconfigure(0, weight=1)
    button_wrap.grid_columnconfigure(0, weight=1)

    button_canvas = tk.Canvas(
        button_wrap,
        bg=palette["panel"],
        highlightthickness=0,
        height=260,
    )
    button_canvas.grid(row=0, column=0, sticky="nsew")

    button_scrollbar = ttk.Scrollbar(
        button_wrap,
        orient="vertical",
        command=button_canvas.yview,
    )
    button_scrollbar.grid(row=0, column=1, sticky="ns")
    button_canvas.configure(yscrollcommand=button_scrollbar.set)

    button_grid = tk.Frame(button_canvas, bg=palette["panel"])
    button_window = button_canvas.create_window((0, 0), window=button_grid, anchor="nw")

    action_bar = tk.Frame(outer, bg=palette["bg"])
    action_bar.pack(fill="x", pady=(12, 10))

    result_header_var = tk.StringVar(value="請先完成上方選擇")
    result_header = tk.Label(
        outer,
        textvariable=result_header_var,
        bg=palette["bg"],
        fg=palette["text"],
        font=("Helvetica", 16, "bold"),
    )
    result_header.pack(anchor="w", pady=(4, 10))

    results_wrap = tk.Frame(outer, bg=palette["bg"])
    results_wrap.pack(fill="both", expand=True)
    results_wrap.grid_rowconfigure(0, weight=1)
    results_wrap.grid_columnconfigure(0, weight=1)

    result_canvas = tk.Canvas(
        results_wrap,
        bg=palette["bg"],
        highlightthickness=0,
    )
    result_canvas.grid(row=0, column=0, sticky="nsew")

    result_scrollbar = ttk.Scrollbar(
        results_wrap,
        orient="vertical",
        command=result_canvas.yview,
    )
    result_scrollbar.grid(row=0, column=1, sticky="ns")
    result_canvas.configure(yscrollcommand=result_scrollbar.set)

    result_container = tk.Frame(result_canvas, bg=palette["bg"])
    result_window = result_canvas.create_window((0, 0), window=result_container, anchor="nw")

    def on_result_configure(_event):
        result_canvas.configure(scrollregion=result_canvas.bbox("all"))

    def on_canvas_configure(event):
        result_canvas.itemconfigure(result_window, width=event.width)

    def on_button_configure(_event):
        button_canvas.configure(scrollregion=button_canvas.bbox("all"))

    def on_button_canvas_configure(event):
        button_canvas.itemconfigure(button_window, width=event.width)

    result_container.bind("<Configure>", on_result_configure)
    result_canvas.bind("<Configure>", on_canvas_configure)
    button_grid.bind("<Configure>", on_button_configure)
    button_canvas.bind("<Configure>", on_button_canvas_configure)

    def update_breadcrumb():
        parts = []
        if state["route"]:
            parts.append(state["route"])
        if state["subroute"]:
            parts.append(state["subroute"])
        if state["direction_label"]:
            parts.append(state["direction_label"])
        if state["stop"]:
            parts.append(state["stop"])
        breadcrumb_var.set(" / ".join(parts) if parts else "尚未開始選擇")

    def clear_buttons():
        for child in button_grid.winfo_children():
            child.destroy()

    def clear_results():
        for child in result_container.winfo_children():
            child.destroy()

    def is_descendant(widget, ancestor):
        current = widget
        while current is not None:
            if current == ancestor:
                return True
            parent_name = current.winfo_parent()
            if not parent_name:
                return False
            current = current._nametowidget(parent_name)
        return False

    def scroll_canvas(canvas, event):
        if event.delta:
            step = -1 if event.delta > 0 else 1
            if root.tk.call("tk", "windowingsystem") == "aqua":
                step = -1 if event.delta > 0 else 1
            canvas.yview_scroll(step, "units")
            return
        if getattr(event, "num", None) == 4:
            canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            canvas.yview_scroll(1, "units")

    def handle_global_mousewheel(event):
        widget = root.winfo_containing(event.x_root, event.y_root)
        if widget is None:
            return
        if is_descendant(widget, button_wrap):
            scroll_canvas(button_canvas, event)
            return "break"
        if is_descendant(widget, results_wrap):
            scroll_canvas(result_canvas, event)
            return "break"
        return

    def set_selection_visibility(visible):
        if visible:
            if subtitle.winfo_manager():
                subtitle.pack_forget()
            if breadcrumb.winfo_manager():
                breadcrumb.pack_forget()
            if step_card.winfo_manager():
                step_card.pack_forget()
            subtitle.pack(anchor="w", pady=(6, 16), before=action_bar)
            breadcrumb.pack(anchor="w", pady=(0, 12), before=action_bar)
            step_card.pack(fill="x", before=action_bar)
            back_button.pack(side="left")
            reset_button.configure(text="重設全部")
            result_canvas.yview_moveto(0)
            button_canvas.yview_moveto(0)
            return

        if subtitle.winfo_manager():
            subtitle.pack_forget()
        if breadcrumb.winfo_manager():
            breadcrumb.pack_forget()
        if step_card.winfo_manager():
            step_card.pack_forget()
        back_button.pack_forget()
        reset_button.configure(text="重新選擇")
        result_canvas.yview_moveto(0)

    def reset_from(level):
        order = ["route", "subroute", "direction", "direction_label", "stop"]
        start = order.index(level)
        for key in order[start:]:
            state[key] = None
        update_breadcrumb()

    def make_option_button(parent, text, command, primary=False):
        bg = palette["accent"] if primary else "#ffffff"
        fg = "#ffffff" if primary else palette["text"]
        active_bg = palette["accent_dark"] if primary else "#f6f3ec"
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=fg,
            relief="flat",
            padx=14,
            pady=10,
            font=("Helvetica", 11, "bold"),
            cursor="hand2",
            wraplength=220,
            justify="center",
        )
        return button

    def render_buttons(options, on_select, columns=3, primary_first=False):
        clear_buttons()
        for index, option in enumerate(options):
            value = option["value"] if isinstance(option, dict) else option
            label = option["label"] if isinstance(option, dict) else str(option)
            button = make_option_button(
                button_grid,
                label,
                lambda selected=value: on_select(selected),
                primary=primary_first and index == 0,
            )
            row = index // columns
            column = index % columns
            button.grid(row=row, column=column, sticky="ew", padx=6, pady=6)
        for column in range(columns):
            button_grid.grid_columnconfigure(column, weight=1)

    def eta_visual(item):
        estimate_time = item.get("EstimateTime")
        stop_status = item.get("StopStatus")
        if estimate_time is not None:
            if estimate_time <= 60:
                return "進站中", palette["arriving"], "NOW"
            if estimate_time <= 180:
                return f"{estimate_time // 60} 分", palette["soon"], "SOON"
            return f"{estimate_time // 60} 分", palette["normal"], "ETA"
        return STOP_STATUS_TEXT.get(stop_status, "暫無資料"), palette["done"], "INFO"

    def render_result_cards(route_name, rows, stop_name=None):
        clear_results()
        if not rows:
            empty = tk.Label(
                result_container,
                text="目前查無符合資料。",
                bg=palette["bg"],
                fg=palette["muted"],
                font=("Helvetica", 12),
            )
            empty.pack(anchor="w", pady=10)
            return

        ordered_rows = sorted(
            rows,
            key=lambda row: (
                row.get("Direction", 99),
                row.get("EstimateTime") is None,
                row.get("EstimateTime", 10**9),
                subroute_text(row),
                row.get("PlateNumb", ""),
                row.get("StopSequence", 10**9),
            ),
        )

        current_direction = None
        for row in ordered_rows:
            direction = row.get("Direction")
            if direction != current_direction:
                current_direction = direction
                section = tk.Label(
                    result_container,
                    text=f"[{direction_text(direction)}]",
                    bg=palette["bg"],
                    fg=palette["accent_dark"],
                    font=("Helvetica", 15, "bold"),
                )
                section.pack(anchor="w", pady=(12, 8))

            eta_text, eta_color, badge_text = eta_visual(row)
            card = tk.Frame(
                result_container,
                bg=palette["panel"],
                highlightbackground=palette["line"],
                highlightthickness=1,
                padx=16,
                pady=14,
            )
            card.pack(fill="x", pady=6)

            top = tk.Frame(card, bg=palette["panel"])
            top.pack(fill="x")

            line_left = tk.Frame(top, bg=palette["panel"])
            line_left.pack(side="left", fill="x", expand=True)

            tk.Label(
                line_left,
                text=subroute_text(row),
                bg=palette["panel"],
                fg=palette["text"],
                font=("Helvetica", 16, "bold"),
            ).pack(anchor="w")

            tk.Label(
                line_left,
                text=get_stop_name(row),
                bg=palette["panel"],
                fg=palette["muted"],
                font=("Helvetica", 11),
            ).pack(anchor="w", pady=(4, 0))

            eta_box = tk.Frame(card, bg=eta_color, padx=14, pady=10)
            eta_box.pack(side="right", anchor="n")

            tk.Label(
                eta_box,
                text=badge_text,
                bg=eta_color,
                fg="#ffffff",
                font=("Helvetica", 9, "bold"),
            ).pack()
            tk.Label(
                eta_box,
                text=eta_text,
                bg=eta_color,
                fg="#ffffff",
                font=("Helvetica", 17, "bold"),
            ).pack()

            meta = tk.Frame(card, bg=palette["panel"])
            meta.pack(fill="x", pady=(12, 0))

            details = [
                f"車牌 {plate_text(row)}",
                f"站序 {row.get('StopSequence', '?')}",
                f"更新 {update_time_text(row)}",
            ]
            if not stop_name:
                details.insert(1, f"站牌 {get_stop_name(row)}")

            tk.Label(
                meta,
                text="  |  ".join(details),
                bg=palette["panel"],
                fg=palette["muted"],
                font=("Helvetica", 10),
            ).pack(anchor="w")

    def show_route_step():
        result_header_var.set("請選主路線")
        step_title_var.set("1. 選擇主路線")
        step_help_var.set("從 1813 或 1815 開始。")
        render_buttons(MAIN_ROUTE_OPTIONS, on_route_selected, columns=2, primary_first=True)

    def show_subroute_step():
        rows = get_route_rows(state["route"])
        subroutes = get_subroutes(rows)
        result_header_var.set(f"已選主路線：{state['route']}")
        step_title_var.set("2. 選擇子路線")
        step_help_var.set("選你要查的支線，例如 1815A、1815B。")
        render_buttons(subroutes, on_subroute_selected, columns=3)

    def show_direction_step():
        rows = get_rows_for_subroute(get_route_rows(state["route"]), state["subroute"])
        options = build_direction_options(rows)
        result_header_var.set(f"{state['route']} / {state['subroute']}")
        step_title_var.set("3. 選擇方向")
        step_help_var.set("選擇往台北或另一端的方向。")
        render_buttons(options, on_direction_selected, columns=2)

    def show_stop_step():
        rows = get_rows_for_subroute(get_route_rows(state["route"]), state["subroute"])
        rows = get_rows_for_direction(rows, state["direction"])
        stop_options = [{"value": None, "label": "全部站點"}]
        stop_options.extend(
            {"value": stop_name, "label": stop_name}
            for stop_name in build_stop_options(rows)
        )
        result_header_var.set(state["direction_label"])
        step_title_var.set("4. 選擇站點")
        step_help_var.set("可選全部站點，或只看某一個站牌。")
        render_buttons(stop_options, on_stop_selected, columns=3, primary_first=True)

    def on_route_selected(route_name):
        reset_from("route")
        state["route"] = route_name
        update_breadcrumb()
        show_subroute_step()

    def on_subroute_selected(subroute_name):
        reset_from("subroute")
        state["subroute"] = subroute_name
        update_breadcrumb()
        show_direction_step()

    def on_direction_selected(option):
        reset_from("direction")
        rows = get_rows_for_subroute(get_route_rows(state["route"]), state["subroute"])
        options = build_direction_options(rows)
        selected = next((item for item in options if item["value"] == option), None)
        state["direction"] = option
        state["direction_label"] = selected["label"] if selected else direction_text(option)
        update_breadcrumb()
        show_stop_step()

    def on_stop_selected(stop_name):
        state["stop"] = stop_name
        update_breadcrumb()
        route_name = state["route"]
        rows = get_rows_for_subroute(get_route_rows(route_name), state["subroute"])
        rows = get_rows_for_direction(rows, state["direction"])
        rows = get_rows_for_stop(rows, stop_name)
        result_header_var.set(
            " / ".join(
                part
                for part in [
                    state["route"],
                    state["subroute"],
                    state["direction_label"],
                    stop_name or "全部站點",
                ]
                if part
            )
        )
        set_selection_visibility(False)
        render_result_cards(route_name, rows, stop_name=stop_name)

    def go_back():
        if state["stop"] is not None:
            reset_from("stop")
            update_breadcrumb()
            show_stop_step()
            return
        if state["direction"] is not None:
            reset_from("direction")
            update_breadcrumb()
            show_direction_step()
            return
        if state["subroute"] is not None:
            reset_from("subroute")
            update_breadcrumb()
            show_subroute_step()
            return
        if state["route"] is not None:
            reset_from("route")
            update_breadcrumb()
            show_route_step()

    def reset_all():
        reset_from("route")
        update_breadcrumb()
        clear_results()
        result_header_var.set("請先完成上方選擇")
        set_selection_visibility(True)
        show_route_step()
        welcome = tk.Label(
            result_container,
            text="請先完成上方選擇。",
            bg=palette["bg"],
            fg=palette["muted"],
            font=("Helvetica", 12),
        )
        welcome.pack(anchor="w", pady=12)

    back_button = make_option_button(action_bar, "上一步", go_back)
    back_button.pack(side="left")
    reset_button = make_option_button(action_bar, "重設全部", reset_all)
    reset_button.pack(side="left", padx=(10, 0))

    root.bind_all("<MouseWheel>", handle_global_mousewheel)
    root.bind_all("<Button-4>", handle_global_mousewheel)
    root.bind_all("<Button-5>", handle_global_mousewheel)

    reset_all()
    root.mainloop()


def main():
    # Main flow:
    # parse args -> get token -> query ETA -> filter -> print
    # 主流程：
    # 讀參數 -> 取 token -> 查 ETA -> 篩選 -> 輸出
    args = parse_args()
    use_interactive = args.interactive or len(sys.argv) == 1
    access_token = get_access_token(
        force_refresh=args.refresh_token,
        debug=args.debug,
    )
    if args.gui:
        run_gui(access_token, debug=args.debug)
        return
    if use_interactive:
        run_interactive_selection(access_token, debug=args.debug)
        return

    route_name = args.route or "1815"
    rows = fetch_eta(route_name, access_token, debug=args.debug)
    rows = filter_by_subroute(rows, args.subroute)
    rows = filter_rows(rows, stop_keyword=args.stop, direction=args.direction)
    print_rows(route_name, rows, stop_keyword=args.stop)


if __name__ == "__main__":
    main()
