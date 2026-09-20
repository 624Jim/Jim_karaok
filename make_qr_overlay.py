#!/usr/bin/env python3
"""生成播放时由 mpv 叠在画面左上角的 QR 图（qr_overlay.png 预览 + qr_overlay.bgra 给 mpv overlay-add 用）。
用法: python3 make_qr_overlay.py [网址] [宽度px，默认256]"""
import sys
import qrcode
from PIL import Image, ImageDraw, ImageFont

url = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.0.249:5000"
W = int(sys.argv[2]) if len(sys.argv) > 2 else 256
k = W / 114.0
H = int(146 * k)
FBOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FREG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
f_t = ImageFont.truetype(FBOLD, int(15 * k))
f_u = ImageFont.truetype(FREG, int(8 * k))

im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle([0, 0, W - 1, H - 1], radius=int(8 * k), fill=(255, 255, 255, 255))
qs = int(90 * k)
qr = qrcode.QRCode(border=1)
qr.add_data(url)
qr.make(fit=True)
qi = qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((qs, qs), Image.NEAREST)
im.paste(qi, (int((W - qs) / 2), int(8 * k)))


def centered(y, font, txt, fill):
    b = d.textbbox((0, 0), txt, font=font)
    d.text((int((W - (b[2] - b[0])) / 2), y), txt, font=font, fill=fill)


centered(int((90 + 14) * k), f_t, "扫码点歌", (20, 20, 30, 255))
centered(int((90 + 33) * k), f_u, url, (80, 90, 105, 255))
im.save("qr_overlay.png")

# mpv overlay-add 要 BGRA 且 alpha 预乘
px = im.load()
buf = bytearray()
for y in range(H):
    for x in range(W):
        r, g, b, a = px[x, y]
        buf += bytes((b * a // 255, g * a // 255, r * a // 255, a))
open("qr_overlay.bgra", "wb").write(bytes(buf))
print("saved qr_overlay.png / qr_overlay.bgra", W, H)
