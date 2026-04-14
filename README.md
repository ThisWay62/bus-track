# Bus TDX API Demo

This project uses Python to query the Taiwan TDX intercity bus API. It currently defaults to route `1815` and prints Estimated Time of Arrival data.

The goal is to develop and verify everything on macOS first, then move it to a Raspberry Pi with an e-ink display for long-term use.

## Project Files

- `bus.py`
  - Main script
  - Reads config, gets token, calls TDX API, and formats output
- `config.example.json`
  - Example config file
  - Safe to commit to GitHub
- `config.json`
  - Your local config file
  - Stores `Client ID` and `Client Secret`
  - Should not be committed
- `.tdx_token_cache.json`
  - Local token cache
  - Prevents requesting a new token every time
  - Should not be committed
- `tdx_debug_log.json`
  - Debug request/response log
  - Should not be committed
- `.gitignore`
  - Excludes local secrets and cache files

## What This Script Does

This script currently queries:

- TDX InterCity Bus API
- Route `1815`
- `EstimatedTimeOfArrival`

It does not scrape a website. It directly calls the API and receives structured JSON data.

This project uses a RESTful API over HTTP:

- resources are represented by URLs
- different HTTP methods have different meanings
- the response body is structured JSON data

Main fields currently used:

- `RouteName`: route name
- `SubRouteName`: subroute name
- `StopName`: stop name
- `StopSequence`: stop order
- `Direction`: outbound or inbound
- `EstimateTime`: estimated time in seconds
- `StopStatus`: stop status
- `UpdateTime`: update time

## Public Data vs Authenticated Access

Although Taiwan bus data sounds like public data, TDX is not an anonymous open URL that anyone can call without setup.

You still need to:

1. register an account
2. create an application
3. receive a `Client ID` and `Client Secret`

You can think of `Client ID` and `Client Secret` as application credentials. They are similar in role to an API key, but the real bus data API is not called by sending those credentials directly to the data endpoint.

The actual flow is:

1. use `Client ID` and `Client Secret` to request a token
2. receive `access_token`
3. use that token in `Authorization: Bearer ...`
4. call the protected bus data API

So the bus endpoint is authorized by token, not directly by the original client credentials.

## TDX Authentication Flow

TDX APIs require authentication before you can query bus data.

Flow:

1. Call the token API with `Client ID` and `Client Secret`
2. TDX returns an `access_token`
3. Put that token into the `Authorization: Bearer ...` header
4. Call the actual bus data API

In practice, the flow is:

```text
POST /token
GET /Bus/EstimatedTimeOfArrival/InterCity
```

## HTTP Method Notes

This project currently uses two HTTP methods:

- `POST`
- `GET`

People often associate `POST` with creating or updating data, but that is not its only use. In this project, `POST` is used to submit credentials to the token endpoint and receive an access token.

`GET` is used to read bus data from the TDX API.

In this project:

- `POST /token`
  - sends `Client ID` and `Client Secret`
  - receives `access_token`
- `GET /Bus/EstimatedTimeOfArrival/InterCity`
  - sends query conditions in the URL
  - receives ETA data

## Why Token Is Required First

The TDX data API is protected.

You cannot just call:

```text
GET https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/InterCity
```

without first obtaining a token and sending it in the `Authorization` header.

In short:

- `Client ID` and `Client Secret` identify your application
- `access_token` is the credential used for the protected data request

## Does the Token Need To Be Requested Every Time?

Not always.

This project caches the token locally, so it does not request a new token on every run.

Cache logic:

- TDX returns `expires_in`
- The script converts it into local `expires_at`
- It stores that value in `.tdx_token_cache.json`

Example:

```json
{
  "access_token": "....",
  "expires_at": 1776262136.011282
}
```

`expires_at` is a Unix timestamp.

The logic is:

- If `time.time() < expires_at`, the token is still valid
- If `time.time() >= expires_at`, the token is expired and a new one is requested

This project also treats the token as expired 5 minutes early to avoid edge-case failures.

## Config

Create `config.json` like this:

```json
{
  "tdx_client_id": "your-client-id",
  "tdx_client_secret": "your-client-secret"
}
```

The script reads `config.json` first. If missing, it falls back to environment variables:

- `TDX_CLIENT_ID`
- `TDX_CLIENT_SECRET`

## Install And Run

Check Python 3:

```bash
python3 --version
```

Basic query:

```bash
cd bus
python3 bus.py --route 1815
```

Print request/response and write them to a log:

```bash
cd bus
python3 bus.py --route 1815 --debug
```

Force refresh token:

```bash
cd bus
python3 bus.py --route 1815 --debug --refresh-token
```

Filter by stop name:

```bash
cd bus
python3 bus.py --route 1815 --stop Taipei
```

Outbound only:

```bash
cd bus
python3 bus.py --route 1815 --direction 0
```

Inbound only:

```bash
cd bus
python3 bus.py --route 1815 --direction 1
```

## Command Reference

```bash
python3 bus.py --route 1815
```

- Query route `1815`
- Print formatted output only

```bash
python3 bus.py --route 1815 --debug
```

- Query route `1815`
- Print request/response details
- Append request/response logs to `tdx_debug_log.json`

```bash
python3 bus.py --route 1815 --debug --refresh-token
```

- Ignore cached token
- Request a new token with `POST`
- Useful when testing authentication flow

```bash
python3 bus.py --route 1815 --stop Taipei
```

- Show only stops with `Taipei` in the stop name

```bash
python3 bus.py --route 1815 --direction 0
```

- Show outbound only

```bash
python3 bus.py --route 1815 --direction 1
```

- Show inbound only

## Debug Log

When you add `--debug`, the script does two things:

1. Prints debug info in the terminal
2. Appends the same data to `tdx_debug_log.json`

Each log entry contains:

- `timestamp`
- `title`
- `payload`

Common titles:

- `TOKEN REQUEST`
- `TOKEN RESPONSE`
- `TOKEN RESPONSE (CACHE)`
- `ETA REQUEST`
- `ETA RESPONSE`

## SSL Certificate Notes

If you install Python from `python.org` on macOS, you may hit an SSL issue when calling HTTPS APIs for the first time:

```text
SSL: CERTIFICATE_VERIFY_FAILED
```

This is usually not a logic bug in the script. It often means your Python certificate bundle is not fully configured yet.

Fix:

```bash
/Applications/Python\ 3.14/Install\ Certificates.command
```

Notes:

- If your Python version is not 3.14, adjust the path
- This installs or fixes the CA bundle used by Python
- After that, HTTPS API requests should work more reliably

## Gzip Response Notes

TDX sometimes returns:

```text
Content-Encoding: gzip
```

That means:

- the response body content is JSON
- but the transport encoding may be gzip-compressed

So `response.read()` should not always be treated as plain UTF-8 text immediately.

If the script reads compressed bytes directly as UTF-8 text, it can fail with:

```text
UnicodeDecodeError
```

This project already handles gzip decompression in `bus.py`.

## Why Route 1815 Shows Repeated Stop Names

Route `1815` may include multiple subroutes, such as:

- `1815`
- `1815B`
- `1815G`

So the same stop name and sequence may appear multiple times across different subroutes or trips.

That is expected API behavior, not a broken response.

For e-ink display usage, you will probably want to narrow the result further by:

- specific stop
- specific direction
- specific subroute

## Files That Should Not Be Committed

Do not commit:

- `config.json`
- `.tdx_token_cache.json`
- `tdx_debug_log.json`
- `__pycache__/`
- `*.pyc`

Why:

- `config.json` may contain API credentials
- `.tdx_token_cache.json` contains access tokens
- `tdx_debug_log.json` may contain request/response logs
- `__pycache__` and `.pyc` are Python cache files

## Development Plan

Recommended order:

1. Verify `bus.py` on macOS
2. Decide which stops, direction, and subroutes you want
3. Simplify output for e-ink display
4. Move it to Raspberry Pi for long-running display

## Possible Next Steps

- show only selected stops
- show only selected subroutes
- output a simplified layout for e-ink display
- add scheduled refresh
- add retry and offline fallback

---

# Bus TDX API Demo 中文說明

這個專案用 Python 查詢台灣 TDX 公路客運 API，目前預設查詢 `1815` 路線的預估到站資訊。

目標是先在 Mac 上開發與驗證，之後再搬到 Raspberry Pi，接墨水屏做長期顯示。

## 專案內容

- `bus.py`
  - 主程式
  - 負責讀設定、取 token、呼叫 TDX API、整理輸出
- `config.example.json`
  - 設定檔範例
  - 可以放上 GitHub
- `config.json`
  - 你本機自己的設定檔
  - 會放 `Client ID` 和 `Client Secret`
  - 不應該 commit
- `.tdx_token_cache.json`
  - token 快取檔
  - 用來避免每次都重新取 token
  - 不應該 commit
- `tdx_debug_log.json`
  - debug 模式下的 request / response 紀錄
  - 不應該 commit
- `.gitignore`
  - 排除本機敏感檔案與快取檔案

## 這個程式在做什麼

目前這支程式查的是：

- TDX 公路客運 API
- 路線 `1815`
- `EstimatedTimeOfArrival`

也就是說，它不是抓網站畫面，也不是爬蟲，而是直接呼叫 API 取得結構化 JSON 資料。

這個專案使用的是 RESTful API：

- 用 URL 表示資源
- 用不同的 HTTP method 表示不同操作
- response body 是結構化 JSON 資料

目前主要會用到的欄位有：

- `RouteName`: 路線名稱
- `SubRouteName`: 子路線名稱
- `StopName`: 站名
- `StopSequence`: 站序
- `Direction`: 去程或返程
- `EstimateTime`: 預估秒數
- `StopStatus`: 站點狀態
- `UpdateTime`: 更新時間

## 公開資料與帳號申請

雖然台灣公車資料聽起來像公開資料，但 TDX 並不是「任何人直接打網址就能查」的匿名介面。

你仍然需要：

1. 註冊帳號
2. 建立 application
3. 取得一組 `Client ID` 和 `Client Secret`

你可以把 `Client ID` 和 `Client Secret` 理解成你的應用程式憑證，概念上有點像 API key，但實際上不是直接把這兩個值丟到公車資料 API 就能查。

真正流程是：

1. 先用 `Client ID` 和 `Client Secret` 去換 token
2. 拿到 `access_token`
3. 再把 token 放到 `Authorization: Bearer ...`
4. 用這個 token 去查真正的公車資料 API

所以真正授權資料查詢的，是 token，不是直接把原始 client credentials 放到資料 endpoint。

## TDX 認證流程

TDX 這類 API 不是直接拿網址就查，而是要先做認證。

流程如下：

1. 先用 `Client ID` 和 `Client Secret` 呼叫 token API
2. TDX 回傳 `access_token`
3. 再把這個 token 放進 `Authorization: Bearer ...` header
4. 接著才能呼叫真正的公車資料 API

也就是實際流程會先：

```text
POST /token
```

再：

```text
GET /Bus/EstimatedTimeOfArrival/InterCity
```

## HTTP Method 差異

這個專案目前主要用到兩種 HTTP method：

- `POST`
- `GET`

很多時候會把 `POST` 跟新增或更新資料聯想在一起，但它不只拿來做資料寫入。在這個專案裡，`POST` 是拿來把憑證送到 token endpoint，換取 `access_token`。

而 `GET` 則是用來讀取公車資料。

在這個專案裡：

- `POST /token`
  - 送出 `Client ID` 和 `Client Secret`
  - 取得 `access_token`
- `GET /Bus/EstimatedTimeOfArrival/InterCity`
  - 把查詢條件放在 URL
  - 取得 ETA 公車資料

## 為什麼要先 call token

因為 TDX 的資料 API 需要授權。

你不能直接只打：

```text
GET https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/InterCity
```

如果沒有先拿 token，或沒有把 token 放進 `Authorization` header，查詢通常會失敗。

換句話說：

- `Client ID` 和 `Client Secret` 是用來辨識你的應用程式
- `access_token` 才是實際呼叫受保護資料 API 的授權憑證

## Token 是不是每次都要拿

不一定。

本專案有做 token 快取，所以不是每次執行都會重新取 token。

快取邏輯：

- TDX token API 會回傳 `expires_in`
- 程式會把它換算成本地的 `expires_at`
- 並存進 `.tdx_token_cache.json`

例如：

```json
{
  "access_token": "....",
  "expires_at": 1776262136.011282
}
```

這裡的 `expires_at` 是 Unix timestamp。

判斷方式是：

- 如果 `time.time() < expires_at`，代表 token 還有效
- 如果 `time.time() >= expires_at`，代表 token 過期，要重新取新的

另外本程式會刻意提前 5 分鐘視為過期，避免剛好壓線失效。

## 設定檔

請建立 `config.json`，內容如下：

```json
{
  "tdx_client_id": "你的-client-id",
  "tdx_client_secret": "你的-client-secret"
}
```

程式會優先讀 `config.json`，如果沒有，才會退回讀環境變數：

- `TDX_CLIENT_ID`
- `TDX_CLIENT_SECRET`

## 安裝與執行

確認 Python 3 可以使用：

```bash
python3 --version
```

基本查詢：

```bash
cd bus
python3 bus.py --route 1815
```

印出 request / response 並寫入 log：

```bash
cd bus
python3 bus.py --route 1815 --debug
```

強制重新取 token：

```bash
cd bus
python3 bus.py --route 1815 --debug --refresh-token
```

只查特定站名：

```bash
cd bus
python3 bus.py --route 1815 --stop 台北
```

只查去程：

```bash
cd bus
python3 bus.py --route 1815 --direction 0
```

只查返程：

```bash
cd bus
python3 bus.py --route 1815 --direction 1
```

## 各個 command 的用途

```bash
python3 bus.py --route 1815
```

- 查詢 `1815` 路線
- 只印整理後的結果

```bash
python3 bus.py --route 1815 --debug
```

- 查詢 `1815`
- 額外印出 request / response
- 也會把 request / response 追加到 `tdx_debug_log.json`

```bash
python3 bus.py --route 1815 --debug --refresh-token
```

- 不使用舊 token
- 重新 `POST` 拿一個新 token
- 適合驗證認證流程時使用

```bash
python3 bus.py --route 1815 --stop 台北
```

- 只顯示站名包含 `台北` 的資料

```bash
python3 bus.py --route 1815 --direction 0
```

- 只顯示去程

```bash
python3 bus.py --route 1815 --direction 1
```

- 只顯示返程

## Debug log 說明

當你加上 `--debug` 時，程式會做兩件事：

1. 在 terminal 印出 debug 資訊
2. 把資料追加到 `tdx_debug_log.json`

每筆 log 會包含：

- `timestamp`
- `title`
- `payload`

常見的 `title` 有：

- `TOKEN REQUEST`
- `TOKEN RESPONSE`
- `TOKEN RESPONSE (CACHE)`
- `ETA REQUEST`
- `ETA RESPONSE`

這樣你之後可以清楚知道某次執行到底發了什麼 request、回了什麼 response。

## SSL 憑證注意事項

如果你是用 `python.org` 的 macOS installer 安裝 Python，第一次連 HTTPS API 時，可能會遇到憑證問題，例如：

```text
SSL: CERTIFICATE_VERIFY_FAILED
```

這通常不是程式邏輯錯，而是本機 Python 憑證鏈還沒補好。

這台專案實測時就遇過這個問題，修法是執行：

```bash
/Applications/Python\ 3.14/Install\ Certificates.command
```

注意：

- 你的 Python 版本如果不是 3.14，路徑要跟著改
- 這個步驟是修正 Python 信任的 CA bundle
- 修好後才比較能正常呼叫 HTTPS API

## Gzip response 注意事項

TDX API 回應有時會帶：

```text
Content-Encoding: gzip
```

這代表：

- response body 的內容本質上是 JSON
- 但 transport 層可能先被 gzip 壓縮

所以不能假設 `response.read()` 回來的內容一定能直接當成 UTF-8 字串處理。

如果程式直接把壓縮資料當成一般 UTF-8 字串讀，可能會出現像這樣的錯誤：

```text
UnicodeDecodeError
```

本專案已經在 `bus.py` 裡處理 gzip 解壓，所以目前這個問題已修正。

## 1815 為什麼會出現很多重複站名

因為 `1815` 底下不一定只有單一資料來源，還可能包含多個子路線，例如：

- `1815`
- `1815B`
- `1815G`

所以同一個站序、同一個站名，可能會因為不同子路線或不同班次而出現多筆資料。

這不是 API 壞掉，而是資料本身就是這樣設計。

如果之後要接墨水屏，通常會再加上：

- 只看特定站牌
- 只看特定方向
- 只看特定子路線

這樣畫面才不會太雜。

## 不要 commit 的檔案

以下檔案不應上傳到 GitHub：

- `config.json`
- `.tdx_token_cache.json`
- `tdx_debug_log.json`
- `__pycache__/`
- `*.pyc`

原因：

- `config.json` 可能含有 API 憑證
- `.tdx_token_cache.json` 含 access token
- `tdx_debug_log.json` 可能含 request / response 紀錄
- `__pycache__` 和 `.pyc` 是 Python 快取檔

## 開發建議

建議開發順序：

1. 先在 Mac 上驗證 `bus.py` 可以正常取 token 與查詢 1815
2. 確認要顯示的站牌、方向、子路線
3. 再把輸出格式改成適合墨水屏
4. 最後搬到 Raspberry Pi 上長期執行

## 後續可以擴充的方向

- 只顯示指定站牌
- 只顯示指定子路線
- 將結果輸出成更適合墨水屏的簡化版文字
- 定時刷新並控制刷新頻率
- 記錄錯誤重試與離線 fallback
