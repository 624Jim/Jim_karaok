# 树莓派点歌机 · 安装 + 发声测试速查

目标：树莓派接 HDMI 萤幕/喇叭发声，手机电脑连网页点歌，声音从树莓派喇叭出。

## 1. 树莓派开机 + 接音效
- 开机接网（有线/Wi-Fi）
- 接上**有喇叭的 HDMI 萤幕/电视**（或 3.5mm 耳机/喇叭）

## 2. 把点歌系统解压到树莓派
用 SSH 或被解压：
```bash
cd ~ && tar -xzf 点歌系统.tar.gz
```
> 目录会变成 `~/点歌系统`，`songs/` 就放歌。

## 3. 装依赖（mpv 是关键，出声靠它）
```bash
sudo apt update
sudo apt install -y python3 python3-pip mpv
pip3 install --user flask qrcode pillow
```

## 4. 启动点歌系统
```bash
cd ~/点歌系统
python3 app.py
```
看到 `Running on http://0.0.0.0:5000` 即成功。

## 5. 查树莓派 IP 供手机/电脑连
```bash
hostname -I
```
例如 `192.168.1.50` → 网页开 `http://192.168.1.50:5000`

## 6. 发声测试（确认喇叭真的有声音）
```bash
# 播一首歌测试，声音应立刻从 HDMI 喇叭出来
mpv ~/点歌系统/songs/测试歌曲-张某某-示例.wav --no-video
```
- ✅ 有声音 = 树莓派发声 OK，接着用网页点歌就会出声
- ❌ 没声音 = 查下面「发声问题」

## 7. 网页点歌测试
手机/电脑连**同一个 Wi-Fi**，
浏览器开 `http://树莓派IP:5000` → 扫码或点进入 → 点「點歌」丢给树莓派播，听树莓派 HDMI 喇叭。

---

## 发声问题速查
- **HDMI 没声音** → 树莓派需把 HDMI 设成输出：
  ```bash
  sudo raspi-config
  # 選 2 System Options → Audio → HDMI
  ```
  或是直接把萤幕/电视音量调大、显卡驱动设 HDMI。
- **萤幕没喇叭** → 改插 3.5mm 耳机/喇叭（`raspi-config → Audio → Headphones`）。
- **手机进不去网页** → 手机和树莓派同一 Wi-Fi、同网段；路由器若开「AP 隔离」要关。

## 开机自动启动（可选）
照 `/home/jim/点歌系统/DEPLOY.md` 第 4 节建 systemd 服务即可。