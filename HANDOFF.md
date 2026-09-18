# 點歌系統（Jukebox Kiosk）交接文件 for Claude Code

## 一、專案是什麼
電視機上盒（Raspberry Pi 5）＋電視：全螢幕播放點歌 MV，畫面左上角烙 QR code。
客人用手機掃 QR → lobby 首頁 → 進「點歌」頁（/dj）→ 選歌播放到電視。

- 手機前端：`templates/index.html`（/dj）、`templates/lobby.html`（/ 入口）
- 後端：Flask `app.py`，port 5000，systemd 服務 `jukebox.service`（需 sudo 重啟）
- 影片播放：mpv 全螢幕，烙印版 `*_qr.mp4` 優先播放
- 系統：Raspberry Pi OS（Debian 13），顯示走 labwc（Wayland）+ Xwayland

## 二、環境與連線
- **Pi**：`jim@192.168.0.249`，密碼 `0000`
- **本機/編輯機**：Linux Mint，專案目錄 `/home/jim/点歌系统/`
- **Songs 目錄**：`/home/jim/点歌系统/songs/`（9 首，含 `*_qr.mp4` 烙印版與 `*_伴奏*` 伴唱）

常用指令（本機執行）：
```bash
# 部署
python3 -m py_compile app.py
sshpass -p '0000' scp app.py templates/index.html jim@192.168.0.249:/home/jim/点歌系统/
sshpass -p '0000' ssh jim@192.168.0.249 'echo 0000 | sudo -S systemctl restart jukebox'
# 看狀態/即時記錄
sshpass -p '0000' ssh jim@192.168.0.249 'systemctl status jukebox'
sshpass -p '0000' ssh jim@192.168.0.249 'echo 0000 | sudo -S journalctl -u jukebox -n 50 --no-pager'
```

## 三、API（都在 port 5000）
- `GET /api/songs`：歌曲清單（已排除 `*_qr`／`*_伴奏*`，含 duration、backing 等）
- `GET /api/play?path=<urllib.parse.quote(絕對路徑)>`：播放
- `POST /api/stop`：停止（**重要**：要真正清掉 mpv 需先 stop 再 `pkill -9 mpv`）
- `GET /api/state`：播放狀態（現在播什麼/進度/磁碟剩餘 disk_xxx/佇列/音量/模式）
- `GET /api/queue`、`/api/volume?v=`、`/api/toggle_mode?mode=`、`/api/prev|next|pause`
- `GET /api/playindex?index=`、`/api/remove?index=`
- `GET /api/ytsearch?q=`、`POST /api/download_url`、`/api/download_progress`
- `GET /api/subtitle?url=`、`GET /api/preview?path=`

## 四、前端注意事項
- `/dj`（index.html）的部分 JS 之前因故被截斷 → 已重建完整：每 2 秒 `refreshState()` 輪詢 `/api/state` 更新狀態列（歌名/進度/點觸動畫、磁碟 GB、佇列數、音量、模式）。
- 本機 **沒有 node**，驗證 JS 語法用 esprima：`pip install --break-system-packages --user esprima`（已安裝）。
- 改完 JS 記得更新 templates。

## 五、播放引擎（mpv）關鍵設定
`_spawn_play` 現役參數（**不要亂改**，這是勘誤整天調出來的唯一順暢組合）：
```
--vo=gpu --gpu-api=opengl --gpu-context=x11egl
--hwdec=v4l2m2m-copy
--ao=alsa --audio-device=alsa/sysdefault:CARD=vc4hdmi   # 聲音強制走 HDMI（預設是 3.5mm 耳機孔＝無聲）
--fullscreen=yes --ontop
--scale=ewa_lanczossharp --dscale=mitchell --scale-radius=3.2   # 升頻（unsharp 濾鏡會掉幀不可用）
```
mpv IPC socket：`/tmp/jukebox-mpv.sock`。
**卡死自癒 watch**（auto_advance 內）：播放滿 12s 起，pos 連續 8s 沒前進 0.6s → 自動殺掉重播同一首（x11egl 偶發卡死）。

## 六、烙 QR（重編碼）流程
- 腳本：Pi 上 `/tmp/burn_hq.py`（本機同名檔在 `/tmp/opencode/burn_hq.py`）
- 參數（**最新、已驗證**）：
```
ffmpeg -y -i <原檔> -i <貼紙> -filter_complex "overlay=20:20" \
  -c:v libx264 -preset veryfast -crf 17 -pix_fmt yuv420p -c:a copy <輸出>_qr.mp4
```
- **地雷：不要加 `-movflags +faststart`**（在 Pi 上會產出 NAL 損壞的壞片，播放卡死在 pos 0）；一定要 `-pix_fmt yuv420p`。
- 貼紙：`/home/jim/点歌系统/qr_sticker_xs2.png`（左上 20:20）
- 舊方法（`h264_v4l2m2m -qp 26` 硬體編碼）因二次壓縮太差已棄用。
- 速度：veryfast 約每首 3.5 分鐘；跑批在 Pi 上 `nohup python3 /tmp/burn_hq.py > /tmp/burn_hq.log &`，log 有每首 START/DONE。

## 七、目前的進度（已完成）
- [x] 全部 9 首已用 **libx264 crf17** 重烙 QR 版（取代舊 v4l2 版），全片 ffmpeg 解碼 **0 錯誤**、貼紙區深色 27%（QR 正常）。
- [x] 聲音改走 HDMI、前端狀態列/磁碟顯示修好、find_songs 排除烙印版＋並行 ffprobe＋開機預熱（/api/songs 7.3s→0.2s）、mpv 卡死 watchdog、升頻 lanczossharp。
- app.py 現役 md5：`205ddb001ba7ceb4b0533841d828ddaf`（已部署、jukebox active 正常）。

## 八、待辦（接續任務）
1. **用戶在電視上目測新畫質**（重烙後尚未人眼確認）。若仍覺得糊＝來源解析度天生只有 320p～480p，可考慮找 720p/1080p 高清來源覆蓋再重烙（用戶尚未決定，先徵求同意）。
2. 若確立高畫質做法：可把 burn_hq.py 整合進後端（例如管理頁面一鍵重烙），目前只是手動跑。
3. 可由你持續優化前端 /dj UI、點歌體驗等（看用戶需求）。

## 九、踩過的坑 / 注意事項
- **ssh 一行指令的 pkill 陷阱**：同一個 ssh 參數列裡不要同時含 mpv 啟動字串與相同樣式的 `pkill -f`，會「自殺」把要啟動的也殺掉；用 PID 或 `[x]` 括號技巧繞開。
- mpv 意外退出時 auto_advance 會自動重播；要徹底停止需先 `/api/stop` 清 state 再 `pkill -9 mpv`。
- Pi 無 socat；測 mpv 用 `/tmp/qry_mpv.py`（固定 sock）或 `/tmp/qry_mpv2.py <socket>`。
- systemctl 操作要帶密碼：`echo 0000 | sudo -S systemctl ...`。
- 烙完新檔讓 songs 目錄 mtime 變動會觸發重掃（已並行+預熱，無感）。