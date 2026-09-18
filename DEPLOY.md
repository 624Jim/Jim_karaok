# 树莓派点歌系统 · 部署与开机启动

## 1. 复制项目到树莓派

把整个 `点歌系统/` 目录复制到树莓派（例如放 `/home/pi/点歌系统`）：

```bash
scp -r 点歌系统 pi@树莓派IP:~/
```

把要播的歌（.mp3/.wav/.flac 等）丢进 `点歌系统/songs/` 目录即可，网页会自动扫描。

## 2. 安装依赖

```bash
sudo apt update
sudo apt install -y python3 python3-pip mpv
pip3 install --user flask qrcode pillow
```

> `mpv` 就是用来播放声音的，一定要装。

## 3. 手动启动测试

```bash
cd ~/点歌系统
python3 app.py
```

看到 `Running on http://0.0.0.0:5000` 就成功了。
用手机连上同一个 Wi-Fi，浏览器打开 `http://树莓派IP:5000` 就会看到二维码入口页。

## 4. 开机自动启动（systemd 服务）

```bash
sudo nano /etc/systemd/system/jukebox.service
```

贴入以下内容（`pi` 请改成你的用户名）：

```ini
[Unit]
Description=Jukebox 点歌系统
After=network.target

[Service]
WorkingDirectory=/home/pi/点歌系统
ExecStart=/usr/bin/python3 /home/pi/点歌系统/app.py
Restart=always
RestartSec=3
User=pi

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
sudo systemctl enable jukebox
sudo systemctl start jukebox
```

检查状态：

```bash
sudo systemctl status jukebox
# 看日志
sudo journalctl -u jukebox -f
```

## 5. 查树莓派 IP

```bash
hostname -I
```

用这个 IP 组二维码即可，例如 `http://192.168.x.x:5000`，直接放电视/墙面，手机扫了进入点歌。

---

## 常见问题

- **没声音** → 确认装了 `mpv`，并检查树莓派默认音效装置有接喇叭/耳机。
- **手机进不去** → 手机和树莓派要在同一个 Wi-Fi；第二台路由器若开启「AP隔离」会阻隔，需关闭。
- **改了代码要重载** → 开发时 `Restart=always` 会自动重启，改完等待几秒即可。