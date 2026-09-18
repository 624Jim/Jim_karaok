# 點歌系統（Jukebox Kiosk）

樹莓派（Raspberry Pi 5）+ 電視的 KTV 點歌機。電視全螢幕播放 MV，畫面左上角烙印「掃碼點歌」QR code；客人用手機掃碼進入點歌頁選歌，歌曲會直接送到電視播放。

## 架構

- **後端**：`app.py`（Flask，port 5000），跑在樹莓派上，systemd 服務名 `jukebox.service`
- **前端**：
  - `templates/lobby.html`　→ `/`　手機掃碼後的入口頁
  - `templates/index.html`　→ `/dj`　點歌 / 播放清單 / 搜尋下載頁
- **播放引擎**：mpv 全螢幕播放，硬體解碼（v4l2m2m），聲音走 HDMI
- **開機畫面**：labwc（Wayland）+ swaybg 顯示 QR 桌布，開機直接進入掃碼畫面（不進系統桌面）
- **歌曲庫**：`songs/`，檔名格式：
  - `歌名.mp4` — 原始檔（無 QR）
  - `歌名_qr.mp4` — 已烙印 QR 的播放版（優先播放這個）
  - `歌名_伴奏.mp3` — 純伴奏音軌（若有）
  - `歌名_伴奏mv.mp4` — 原畫面+純伴奏音軌 合成檔（伴唱模式用，自動產生）

## 環境

| | |
|---|---|
| 樹莓派 | `jim@192.168.0.249`，密碼 `0000` |
| 本機開發目錄 | `/home/jim/点歌系统/` |
| 樹莓派專案目錄 | `/home/jim/点歌系统/`（路徑相同） |

## 部署流程（本機改完後）

```bash
# 1. 驗證語法
python3 -m py_compile app.py

# 2. 部署（下載/燒錄任務進行中不要重啟，會把 ffmpeg 一起殺掉）
sshpass -p '0000' scp app.py templates/index.html jim@192.168.0.249:/home/jim/点歌系统/
sshpass -p '0000' scp templates/index.html jim@192.168.0.249:/home/jim/点歌系统/templates/

# 3. 重啟服務
sshpass -p '0000' ssh jim@192.168.0.249 'echo 0000 | sudo -S systemctl restart jukebox'

# 4. 看狀態 / 即時記錄
sshpass -p '0000' ssh jim@192.168.0.249 'systemctl status jukebox'
sshpass -p '0000' ssh jim@192.168.0.249 'echo 0000 | sudo -S journalctl -u jukebox -n 50 --no-pager'
```

## 主要 API（port 5000）

| 端點 | 說明 |
|---|---|
| `GET /api/songs` | 歌曲清單（含 duration、has_backing 等） |
| `GET /api/play?path=` | 點歌播放（path 需在 songs 目錄內，已做路徑穿越防護） |
| `POST /api/stop` | 停止播放 |
| `GET /api/state` | 目前播放狀態（歌名/進度/佇列/音量/模式） |
| `GET /api/queue` `/api/remove?index=` | 播放佇列 |
| `GET /api/prev` `/api/next` `/api/pause` | 播放控制 |
| `GET /api/toggle_mode?mode=lead\|backing` | 切換導唱 / 伴奏 |
| `GET /api/ytsearch?q=` | YouTube 搜尋 |
| `POST /api/download_url` `GET /api/download_progress` | 下載新歌（下載完自動燒 QR 才入庫，見下） |
| `GET /api/find_backing?path=` `POST /api/upload_backing` | 取得/上傳純伴奏 |
| `GET /api/subtitle?path=` | 歌詞（供手機同步顯示） |
| `GET /api/preview?path=` | 手機試聽 |

## 下載新歌自動燒 QR

透過 `/dj` 頁面「搜尋 → 下載」新增的歌曲，下載完成後會**自動燒錄 QR**（呼叫 `_burn_qr()`，跟手動 `burn_hq.py` 用同一組 ffmpeg 參數），整個過程可能要幾分鐘，前端會顯示「🔳 烙印 QR code 中」。燒完才會進播放佇列，第一次播放就一定有 QR，不需要手動處理。

貼紙圖檔：`qr_sticker_xs2.png`（白底，左上 20:20 疊加）。

## 開機直接進 QR 掃碼畫面

- `~/.config/labwc/autostart`：開機用 `swaybg` 顯示 `qr_wall.png`
- `/etc/xdg/labwc/autostart`（系統層級）：已移除 `pcmanfm-pi --desktop` 和 `wf-panel-pi`，避免標準桌面（圖示/工作列）蓋掉 QR 畫面。原始備份在同目錄 `autostart.orig_backup`。

## 安全性

`/api/play`、`/api/delete`、`/api/preview`、`/api/find_backing`、`/api/upload_backing`、`/api/subtitle` 等端點對局域網內任何手機開放（無登入驗證），一律用 `_safe_songs_path()` 把傳入路徑正規化並限制在 `songs/` 目錄內，防止路徑穿越。

## 已知注意事項

- mpv 播放參數（HDMI 音效、hwdec、升頻濾鏡）是調校過的組合，**不要亂改**
- 燒 QR 千萬別加 `-movflags +faststart`（會在 Pi 上產生卡死的壞檔）
- `_ensure_loudness` / `_backing_mv` 已加鎖防止同首歌被重複觸發多個 ffmpeg 任務
- ssh 一行指令避免同時出現啟動字串跟同樣式的 `pkill -f`，會把自己的 ssh session 一起殺掉；用 `pkill -x` 精確比對程序名
