import os
import re
import io
import json
import socket
import time
import shutil
import threading
import subprocess
import signal
from flask import Flask, render_template, request, jsonify, send_from_directory, Response, send_file, redirect

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SONGS_DIR = os.path.join(BASE_DIR, 'songs')
PORT = int(os.environ.get('PORT', 5000))
# 播放时由 mpv 叠在左上角的 QR 图（raw BGRA，尺寸/位置为屏幕像素，1080p 屏）；由 make_qr_overlay.py 生成
QR_OVERLAY = os.path.join(BASE_DIR, 'qr_overlay.bgra')
QR_OVERLAY_W, QR_OVERLAY_H, QR_OVERLAY_POS = 256, 327, (45, 45)


def _safe_songs_path(path):
    """把用户传入的 path 正规化并确认落在 SONGS_DIR 内，否则回 None。
    这个 API 对局域网内任何手机开放，用 startswith(SONGS_DIR) 做字串比对
    会被 '../../etc/xxx' 這種相對路徑片段繞過，一定要先 realpath 再比对。"""
    if not path:
        return None
    real = os.path.realpath(path)
    root = os.path.realpath(SONGS_DIR) + os.sep
    return real if real.startswith(root) else None

# 音量持久化文件（重启后保持上次音量）
VOLUME_FILE = os.path.join(BASE_DIR, '.volume')

# 歌曲使用记录（last播放时间/次数/下載時間），供自動清理判斷
USAGE_FILE = os.path.join(BASE_DIR, '.usage.json')

# 自動清理門檻：硬碟「已用」超過 20G 才開始自動刪（29G 總量，約 9G 空間時觸發），
# 一直清到已用降到 17G 為止。優先刪「很久沒播/沒載」的歌，同級先刪大檔。
CLEAN_TRIGGER_USED = 20 * 1024 ** 3
CLEAN_TARGET_USED = 17 * 1024 ** 3
OLD_DAYS = 10  # 10 天內播過或下載過的歌不自動刪

# 歌曲响度补偿：用 ebur128 侦测每首歌的实际响度（integrated LUFS），
# 偏小聲的自动加增益（最多 +12dB），不小聲的不动。结果缓存避免重复测量。
LOUD_FILE = os.path.join(BASE_DIR, '.loudness.json')
TARGET_LUFS = -14.0      # 目标响度（流行音乐母带大约在此附近）
MAX_GAIN_DB = 12.0       # 过小声音歌最多补 12dB，避免过度放大失真


def _load_volume():
    try:
        with open(VOLUME_FILE) as f:
            return max(0, min(100, int(f.read().strip())))
    except Exception:
        return 100

# 支持的音/视频扩展名
AUDIO_EXTS = {'.mp3', '.wav', '.flac', '.m4a', '.ogg', '.aac', '.wma'}
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv'}
SUBTITLE_EXTS = {'.srt', '.vtt', '.ass', '.ssa'}

# 使用用户目录下的最新版 yt-dlp（避免系统旧版无法下载 YouTube）
YTDLP = os.path.expanduser('~/.local/bin/yt-dlp')
if not os.path.isfile(YTDLP):
    YTDLP = 'yt-dlp'

# 播放状态
state = {
    'queue': [],          # 播放队列（歌曲路径列表）
    'current_index': -1,  # 当前播放的队列索引
    'playing': False,
    'paused': False,      # 使用者手动暂停（自动推进线程看到它就不要擅自恢复播放）
    'volume': _load_volume(),   # 默认音量（从持久化文件读取）
    'current': None,      # 当前歌曲信息
    'mode': 'lead',       # 'lead'=导唱(人声) 'backing'=伴奏
}

# 使用 mpv 播放（树莓派上最稳，若没有则退回默认播放器）
player_proc = None
player_lock = threading.Lock()

# 背景任务去重：同一路径的响度测量/伴奏合成，同时只允许一份在跑
# （原本没有这层保护，快速连点切换会让同一首歌同时起好几个 ffmpeg，把系统拖垮）
_measuring = set()
_measuring_lock = threading.Lock()
_job_locks_guard = threading.Lock()
_job_locks = {}


def _get_job_lock(key):
    with _job_locks_guard:
        lk = _job_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _job_locks[key] = lk
        return lk


def _disk_usage():
    """返回 (总量, 已用, 剩余) 字节"""
    du = shutil.disk_usage(SONGS_DIR)
    return du.total, du.used, du.free


def _load_usage():
    try:
        with open(USAGE_FILE, encoding='utf-8') as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_usage(d):
    try:
        with open(USAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def _mark_usage(path, field='last'):
    """记录某首歌曲的播放/下载时间，供自动清理判断新旧。"""
    if not path:
        return
    try:
        d = _load_usage()
        e = d.get(path) or {}
        if field == 'last':
            e['last'] = time.time()
            e['plays'] = e.get('plays', 0) + 1
            e.setdefault('added', time.time())
        else:
            e.setdefault('added', time.time())
        d[path] = e
        _save_usage(d)
    except Exception:
        pass

# ===== 响度补偿 =====

def _load_loudness():
    try:
        with open(LOUD_FILE, encoding='utf-8') as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_loudness(d):
    try:
        with open(LOUD_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def _gain_db(path):
    """从缓存取这首歌的增益(dB)；没测过或文件已变动返回 0。"""
    try:
        e = _load_loudness().get(path)
        if not e:
            return 0.0
        if e.get('m') != int(os.path.getmtime(path)) or e.get('s') != os.path.getsize(path):
            return 0.0
        return float(e.get('gain', 0.0))
    except Exception:
        return 0.0


def _measure_loudness(path):
    """后台测量整首歌的 integrated loudness(EBU R128)，算出補償增益並缓存。"""
    try:
        p = subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-nostats', '-i', path,
             '-af', 'ebur128=framelog=verbose', '-f', 'null', '-'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            _, err = p.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except Exception:
                pass
            return
        lufs = None
        seen_summary = False
        for line in err.splitlines():
            if 'Integrated loudness' in line:
                seen_summary = True
            elif seen_summary and 'I:' in line and 'LUFS' in line:
                m = re.search(r'I:\s*(-?\d+(?:\.\d+)?)', line)
                if m:
                    lufs = float(m.group(1))
                break
        if lufs is None:
            _dbg('[loud] %s 無法測出响度' % os.path.basename(path))
            return
        gain = max(0.0, min(MAX_GAIN_DB, TARGET_LUFS - lufs))
        st = os.stat(path)
        d = _load_loudness()
        d[path] = {'gain': round(gain, 1), 'lufs': round(lufs, 1),
                   'm': int(st.st_mtime), 's': st.st_size}
        _save_loudness(d)
        _dbg('[loud] %s lufs=%.1f -> gain=+%.1f dB' % (os.path.basename(path), lufs, gain))
    except Exception as e:
        _dbg('[loud] measure error: %r' % (e,))


def _af_with_gain(af, gain_db):
    """把响度补偿接在現有音頻鏈後（X-MIX/無濾鏡 都可）。"""
    if not gain_db or gain_db <= 0:
        return af
    vol = 'lavfi=[volume=%+.1fdB]' % gain_db
    return (af + ',' + vol) if af else vol


def _ensure_loudness(path):
    """需要时后台测量这首歌；測完若它还在播，立即套用补偿（不影響當下音量旋鈕）。"""
    try:
        e = _load_loudness().get(path)
        if e and e.get('m') == int(os.path.getmtime(path)) and e.get('s') == os.path.getsize(path):
            return
    except Exception:
        pass

    with _measuring_lock:
        if path in _measuring:
            return  # 已有一份在测，不重复起
        _measuring.add(path)

    def job():
        try:
            _measure_loudness(path)
            try:
                with player_lock:
                    cur = state['current']
                    m = state['mode'] if state['playing'] else None
                if cur and cur.get('path') and os.path.exists(MPV_SOCK) and m:
                    play_path = current_play_path(cur)
                    if play_path != path or state['playing'] is not True:
                        return
                    g = _gain_db(play_path)
                    af = _af_with_gain(_mpv_af(m, play_path), g)
                    ipc_send(['set', 'af', af or ''])
                    _dbg('[loud] 即時套用: af=%r' % (af,))
            except Exception as e:
                _dbg('[loud] 即時套用 error: %r' % (e,))
        finally:
            with _measuring_lock:
                _measuring.discard(path)
    threading.Thread(target=job, daemon=True).start()

# 時長快取（key = (path, size, mtime)）避免每秒 ffprobe
_dur_cache = {}
_DUR_CACHE_MAX = 500

def _get_duration(path):
    try:
        st = os.stat(path)
        key = (path, st.st_size, int(st.st_mtime))
        if key in _dur_cache:
            return _dur_cache[key]
    except OSError:
        return None
    try:
        out = subprocess.run(
            ['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0', path],
            capture_output=True, text=True, timeout=30)
        val = out.stdout.strip()
        d = float(val) if val else None
    except Exception:
        d = None
    if d is not None and len(_dur_cache) < _DUR_CACHE_MAX:
        _dur_cache[key] = d
    return d

def _probe_vcodec(path):
    """返回流0 video codec name (如 h264/hevc/av1)，无法辨識回空字串。"""
    try:
        out = subprocess.run(
            ['ffprobe','-v','error','-select_streams','v:0',
             '-show_entries','stream=codec_name','-of','csv=p=0', path],
            capture_output=True, text=True, timeout=20)
        return out.stdout.strip().lower()
    except Exception:
        return ''

@app.route('/api/preview')
def api_preview():
    """手機試聽端點：若 mp4/h264 直接轉址，否則用 ffmpeg 流式轉檔供手機播。"""
    path = _safe_songs_path(request.args.get('path',''))
    if not path:
        return 'no', 404
    if not os.path.isfile(path):
        return 'no', 404
    # h264 + aac 的 mp4/mkv 直接轉址（手機原生播）
    codec = _probe_vcodec(path)
    if codec in ('h264', ''):
        rel = os.path.relpath(path, SONGS_DIR).replace(os.sep, '/')
        from urllib.parse import quote
        return redirect('/songs/' + quote(rel))
    # 其他格式（av1/hevc/vp9/flv/mkv）轉 h264+aac mp4 串流
    def gen():
        try:
            proc = subprocess.Popen(
                ['ffmpeg','-hide_banner','-loglevel','error','-i', path,
                 '-f','mp4','-movflags','frag_keyframe+empty_moov+default_base_moof',
                 '-c:v','libx264','-preset','veryfast','-crf','28',
                 '-c:a','aac','-b:a','96k','-sn',
                 '-threads','2','pipe:1'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                if proc.poll() is None:
                    proc.kill()
        except Exception:
            pass
    return Response(gen(), mimetype='video/mp4',
                    headers={'Cache-Control':'no-store',
                             'Access-Control-Allow-Origin':'*'})


def _maybe_cleanup():
    """自动清理引擎：硬碟用太多时，按「旧+不常用」优先、同級删大檔，删到目標水位。"""
    try:
        total, used, free = _disk_usage()
        if used < CLEAN_TRIGGER_USED:
            return
        _dbg('[cleanup] 已用 %.1fG 触发清理' % (used / 1024 ** 3))
        songs = find_songs()
        usage = _load_usage()
        now = time.time()
        protected = set()
        with player_lock:
            for s in state['queue']:
                protected.add(s.get('path'))
        cand = []
        for s in songs:
            p = s.get('path')
            if not p or p in protected:
                continue
            size = s.get('size') or 0
            if size <= 0:
                continue
            u = usage.get(p, {})
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                mtime = 0
            last = u.get('last') or u.get('added') or mtime  # 没播过就按下载时间算
            cand.append((last, -size, p))
        # 最久没播的在最前面，同一批里先删大的
        cand.sort()
        for last, neg_size, p in cand:
            used = _disk_usage()[1]
            if used < CLEAN_TARGET_USED:
                break
            if now - last < OLD_DAYS * 86400:
                continue  # 太新鲜，不删
            size = -neg_size
            _delete_song_files({'path': p, 'backing': find_backing(p)})
            usage.pop(p, None)
            _save_usage(usage)
            _dbg('[cleanup] 自动删除 旧/不常用 大档: %s (%.1fM)' % (
                os.path.basename(p), size / 1024 ** 2))
    except Exception as e:
        _dbg('[cleanup] error: %r' % (e,))
auto_thread = None
auto_running = True

# 后台下载任务（供前端进度条轮询）
dl_job = {
    'busy': False, 'done': False, 'error': '',
    'log': '/tmp/jukebox-dl.log',
    'percent': 0.0, 'speed': '', 'eta': '',
    'name': '', 'index': -1,
    'force_play': False,
}


def find_subtitle(media_path):
    """查找与媒体文件相匹配的字幕文件（字幕名可带语言后缀或视频带 .h264/.720p 标记）"""
    base = os.path.splitext(media_path)[0]
    for ext in SUBTITLE_EXTS:
        sub_path = base + ext
        if os.path.isfile(sub_path):
            return sub_path
    dir_path = os.path.dirname(media_path)
    media_key = os.path.basename(base).lower()
    for mark in ('.h264', '.720p', '.480p', '_720p', '_480p'):
        if media_key.endswith(mark):
            media_key = media_key[: -len(mark)]
            break
    # 弱匹配：字幕文件名以媒体名为前缀（允许语言后缀等杂讯）
    for f in sorted(os.listdir(dir_path)):
        low = f.lower()
        if not low.endswith(tuple(SUBTITLE_EXTS)):
            continue
        sub_base = os.path.splitext(f)[0].lower()
        if sub_base == media_key or sub_base.startswith(media_key):
            return os.path.join(dir_path, f)
    return None


_BACKING_EXTS = ('.mp3', '.m4a', '.mp4', '.wav', '.flac', '.ogg', '.aac', '.opus', '.webm', '.mkv')


def find_backing(media_path):
    """查找同名伴奏版（歌名_伴奏.* 不限副檔名）"""
    base = os.path.splitext(media_path)[0]
    for ext in _BACKING_EXTS:
        backing = base + '_伴奏' + ext
        if os.path.isfile(backing):
            return backing
    return None


_songs_cache = {'mtime': None, 'list': None}


def find_songs():
    """扫描 songs 目录，返回歌曲列表（按目录 mtime 缓存，加速重复搜索）"""
    try:
        mtime = os.stat(SONGS_DIR).st_mtime
    except OSError:
        mtime = None
    if _songs_cache['mtime'] == mtime and _songs_cache['list'] is not None:
        return _songs_cache['list']
    songs = []
    # 构建转码产物映射：只有当原文件 base.mp4 也存在时，才认为 .h264/.720p/.480p 是转码产物
    # 标记格式：xxx.h264.mp4 / xxx.720p.mp4 / xxx.480p.mp4 / xxx_480p.mp4
    all_files = set()
    if os.path.isdir(SONGS_DIR):
        all_files = set(os.listdir(SONGS_DIR))
    conv_map = {}  # 原文件base -> 转码产物文件名
    for f in sorted(all_files):
        low = f.lower()
        for mark in ('.720p.mp4', '.480p.mp4', '_720p.mp4', '_480p.mp4'):
            if low.endswith(mark):
                base = f[:-len(mark)]
                if (base + '.mp4') in all_files:
                    conv_map.setdefault(base, f)
                    break
        if low.endswith('.h264.mp4'):
            base = f[:-len('.h264.mp4')]
            if (base + '.mp4') in all_files:
                conv_map.setdefault(base, f)
    songs = []
    if os.path.isdir(SONGS_DIR):
        for f in sorted(os.listdir(SONGS_DIR)):
            ext = os.path.splitext(f)[1].lower()
            if ext not in AUDIO_EXTS and ext not in VIDEO_EXTS:
                continue
            name0 = os.path.splitext(f)[0]
            if name0.endswith('_伴奏') or '_伴奏mv' in name0:
                continue
            if name0.endswith('_qr'):
                continue
            if name0 in conv_map and f != conv_map[name0]:
                continue
            path = os.path.join(SONGS_DIR, f)
            name = name0
            for mark in ('.h264.mp4', '.h264', '.720p.mp4', '.720p', '.480p.mp4', '.480p'):
                if name.lower().endswith(mark):
                    name = name[:-len(mark)]
                    break
            artist = '未知'
            title = name
            m = re.split(r'[-_—–]', name, maxsplit=1)
            if len(m) == 2:
                artist = m[0].strip()
                title = m[1].strip()
            subtitle = find_subtitle(path)
            backing = find_backing(path)
            low_name = name.lower()
            backing_source = any(k in low_name for k in (
                '原版伴奏', 'karaoke version', '纯伴奏', 'slow版伴奏'))
            songs.append({
                'name': name,
                'path': path,
                'artist': artist,
                'title': title,
                'is_video': ext in VIDEO_EXTS,
                'subtitle': subtitle,
                'backing': backing,
                'has_backing': bool(backing),
                'backing_source': backing_source,
                'size': os.path.getsize(path),
                'duration': None,
                'url': '/songs/' + os.path.relpath(path, SONGS_DIR).replace(os.sep, '/'),
            })
    # 并行探测时长（每首一次 ffprobe，顺序做全量扫描很慢）
    try:
        from concurrent.futures import ThreadPoolExecutor
        paths = [s['path'] for s in songs]
        with ThreadPoolExecutor(max_workers=min(8, len(paths) or 1)) as ex:
            durs = list(ex.map(_get_duration, paths))
        for s, d in zip(songs, durs):
            s['duration'] = d
    except Exception:
        for s in songs:
            if not s.get('duration'):
                s['duration'] = _get_duration(s['path'])
    _songs_cache['mtime'] = mtime
    _songs_cache['list'] = songs
    return songs




MPV_SOCK = '/tmp/jukebox-mpv.sock'
# 播放開始時間（秒級），供 auto_advance 判斷是否「真正播了一段」才決定刪檔
spawn_ts = 0.0
USE_IPC_SWITCH = True


def _daemon_alive():
    with player_lock:
        return (player_proc is not None and player_proc.poll() is None
                and os.path.exists(MPV_SOCK))
_MIN_PLAY_SECS = 3  # 至少播過 3 秒才納入「正常結束」考慮（防崩潰/秒退誤刪）
AF_DEFAULT = ''
# 伴唱滤链（⑥号方案「分频保留低频」）：
# 低频(<220Hz)原样保留保住鼓/Bass，高频段做中声道人声抽除（stereotools mlev=0.06），
# 再与低频合并。人声残留在原声-18dB 基础上再降约 18dB，低频率响应≈原声。
AF_XMIX = ('lavfi=[asplit=2[a][b];[a]lowpass=f=220[lo];[b]highpass=f=220,'
           'stereotools=mlev=0.06:slev=1[hi];[lo][hi]amerge=inputs=2,'
           'pan=stereo|c0=c0+c2|c1=c1+c3]')


def ipc_send(cmd, timeout=3, quiet=False):
    """通过 IPC 给常驻 mpv 发命令。返回响应 dict；失败返回 None。quiet=True 时静默失败（探查用）。"""
    rid = int(time.time() * 1000000)
    payload = {'command': cmd, 'request_id': rid}
    try:
        import socket as _sock
        s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(MPV_SOCK)
        s.sendall((json.dumps(payload) + '\n').encode())
        buf = b''
        while b'\n' not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        line = buf.split(b'\n', 1)[0].decode(errors='replace')
        if not line:
            return None
        d = json.loads(line)
        return d
    except Exception as e:
        if not quiet:
            with open('/tmp/jukebox-debug.log', 'a') as f:
                f.write('[ipc] %r: %s\n' % (cmd, e))
        return None


def _dbg(msg):
    try:
        with open('/tmp/jukebox-debug.log', 'a') as f:
            f.write('[dbg] %s\n' % msg)
    except Exception:
        pass


def _mpv_time():
    """從常駐 mpv 讀目前播放位置與片長（秒）。未在播/daemon 掛了回 (None, None)。"""
    if not state['playing'] or not os.path.exists(MPV_SOCK):
        return None, None
    pos = dur = None
    r = ipc_send(['get_property', 'time-pos'], quiet=True)
    if r and r.get('error') in (None, 'success'):
        pos = r.get('data')
    r = ipc_send(['get_property', 'duration'], quiet=True)
    if r and r.get('error') in (None, 'success'):
        dur = r.get('data')
    try:
        pos = float(pos) if pos is not None else None
    except (TypeError, ValueError):
        pos = None
    try:
        dur = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur = None
    return pos, dur


def _mpv_af(mode, path):
    """根据模式返回音频滤镜。
    伴奏模式：若播的不是真伴奏文件(名含 _伴奏)即用 X-MIX 消居中声道人声——
    任何歌（含「導唱」文件）都能切出纯伴奏，符合 KTV 习惯。"""
    if mode == 'backing':
        if '_伴奏' not in os.path.basename(path):
            return AF_XMIX
    return AF_DEFAULT


def _kill_player():
    """收尸当前 player（若在跑先等 1s，超时 SIGKILL），并清掉 socket。锁内安全。"""
    global player_proc
    with player_lock:
        p = player_proc
        player_proc = None
        try:
            os.unlink(MPV_SOCK)
        except OSError:
            pass
    if p is not None:
        # 还活着就先 SIGTERM 让 mpv 立刻退出；原本是空等 1 秒才强杀，切歌白白慢 1 秒
        if p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
        try:
            p.wait(timeout=0.4)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            try:
                p.kill()
            except OSError:
                pass
            try:
                p.wait(timeout=2)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
    return p


def _spawn_play(path, mode, af, sub_override=None):
    """可靠路径：单次启动 mpv（gpu/wayland，全屏等比适配屏幕 + --ontop）。
    daemon（IPC 秒切）存活时才走 IPC；daemon 挂了直接走这里。"""
    global player_proc, spawn_ts
    spawn_ts = time.time()
    sub = sub_override or find_subtitle(path)
    cmd = [
        'mpv', '--vo=gpu', '--gpu-api=opengl', '--gpu-context=x11egl',
        '--hwdec=v4l2m2m-copy',
        '--ao=alsa', '--audio-device=alsa/sysdefault:CARD=vc4hdmi',
        f'--volume={state["volume"]}', '--volume-max=100',
        '--no-terminal', '--really-quiet', '--osd-level=0',
        '--fullscreen=yes', '--ontop',
        '--scale=ewa_lanczossharp', '--dscale=mitchell',
        '--scale-radius=3.2',
        '--input-ipc-server=' + MPV_SOCK,
    ] + (['--af=' + af] if af else [])
    if sub:
        cmd.append(f'--sub-file={sub}')
    cmd.append(path)
    _dbg('spawn: ' + ' '.join(cmd))
    _kill_player()  # 先停掉旧 player，避免双实例叠播
    subprocess.run(['pkill', '-9', '-x', 'mpv'])  # 保险：清掉任何残留实例（含竞态产生）
    with player_lock:
        try:
            env = dict(os.environ)
            env['DISPLAY'] = ':0'
            player_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=env)
            state['playing'] = True
        except (subprocess.CalledProcessError, FileNotFoundError):
            player_proc = None
            state['playing'] = False


def play(path, mode=None, sub=None, audio_path=None, mark_path=None):
    """播放指定文件：IPC daemon 恰好活着才走秒切；否则直接单次启动（可靠、全屏）。

    path        实际送入 mpv 的文件（可能是合成档/<伴奏>）
    sub         外挂字幕（合成档的字幕来自原 MV）
    audio_path  真正出声的文件（用于响度/增益），默认 = path
    mark_path   播放纪录（played）记到哪首歌，默认 = path
    """
    global player_proc, spawn_ts
    m = mode if mode is not None else state['mode']
    ap = audio_path or path
    af = _af_with_gain(_mpv_af(m, ap), _gain_db(ap))
    _ensure_loudness(ap)  # 没量过的先量（后台），量完若仍在播即套用
    mp = mark_path or path
    _dbg('play: path=%s mode=%s' % (os.path.basename(path), m))
    daemon_ok = USE_IPC_SWITCH and _daemon_alive()
    if not daemon_ok:
        _spawn_play(path, m, af, sub)
        _add_qr_overlay()
        _mark_usage(mp)
        return
    spawn_ts = time.time()
    ipc_send(['set', 'af', af or ''])
    r = ipc_send(['loadfile', path, 'replace'])
    _dbg('play: loadfile r=%r' % (r,))
    if r is None or r.get('error') not in (None, 'success'):
        # daemon 暴毙：收尸后直接走可靠的单次启动
        _dbg('play: daemon坏掉,收尸走单次启动')
        _kill_player()
        _spawn_play(path, m, af, sub)
        _add_qr_overlay()
        return
    if sub:
        ipc_send(['sub-add', sub, 'select'])
    ipc_send(['set_property', 'volume', state['volume']])
    _add_qr_overlay()
    with player_lock:
        state['playing'] = True
    _mark_usage(mp)


def stop_player():
    """停止当前播放：退出 mpv，无闲置窗遮挡，点歌界面立刻露出"""
    global player_proc
    _kill_player()
    state['playing'] = False
    state['paused'] = False


def _add_qr_overlay():
    """后台把 QR 叠加图送进 mpv（等 mpv 的 IPC 起来；重复添加同 id 只是覆盖）。"""
    if not os.path.isfile(QR_OVERLAY):
        return

    def job():
        w, h = QR_OVERLAY_W, QR_OVERLAY_H
        for _ in range(40):
            r = ipc_send(['overlay-add', 1, QR_OVERLAY_POS[0], QR_OVERLAY_POS[1], QR_OVERLAY,
                          0, 'bgra', w, h, w * 4], timeout=1, quiet=True)
            if r is not None and r.get('error') == 'success':
                return
            time.sleep(0.25)
        _dbg('qr-overlay: 10 秒内没能叠上')
    threading.Thread(target=job, daemon=True).start()


def current_play_path(song):
    """根据模式返回要播放的路径：导唱=原文件，伴奏=同名 _伴奏 文件"""
    if state['mode'] == 'backing' and song.get('backing'):
        return song['backing']
    return song['path']


def play_current(new_mode=None):
    """播放队列当前歌曲。new_mode 用于立即切换原声/伴奏。
    伴奏模式：优先「原MV影像+纯伴奏音轨」合成档；没有伴奏文件则播原文件+实时人声消除滤波器。"""
    if 0 <= state['current_index'] < len(state['queue']):
        song = state['queue'][state['current_index']]
        state['current'] = song
        state['playing'] = True
        state['paused'] = False
        m = new_mode if new_mode is not None else state['mode']
        sub = song.get('subtitle') or find_subtitle(song['path'])
        if m == 'backing' and song.get('backing'):
            mv = _backing_mv(song)
            if mv:
                play(mv, mode=m, sub=sub, audio_path=song['backing'], mark_path=song['path'])
                return True
            play(song['backing'], mode=m, sub=sub, mark_path=song['path'])
        else:
            play(song['path'], mode=m, sub=sub, mark_path=song['path'])
        return True
    return False


def get_lan_ip():
    """获取本机局域网 IP（供二维码使用）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def get_qr_url():
    """生成点歌页面的 URL（二维码内容）"""
    ip = get_lan_ip()
    return f'http://{ip}:{PORT}'


def _backing_mv(song):
    """若原曲是影片且有纯伴奏，产生「原MV影像+纯伴奏音轨」的合成档（歌名_伴奏mv.mp4）。

    伴奏模式不再直接播 mp3（那样电视只剩一张封面图），而是把原 MV 的画面保留、
    音频换成纯伴奏——这才是 KTV 的伴唱画面。返回合成档路径，失败回 None。"""
    src = (song or {}).get('path')
    bak = (song or {}).get('backing')
    if not src or not bak or not os.path.isfile(bak):
        return None
    if os.path.splitext(src)[1].lower() not in VIDEO_EXTS:
        return None  # 原曲本身没画面：直接播伴奏音频
    base = os.path.splitext(src)[0]
    out = base + '_伴奏mv.mp4'
    # 同一个输出档同时只允许一个 ffmpeg 在合成：短时间内重复点切换伴奏时，
    # 后来的请求排队等第一个做完，而不是各自起一份 ffmpeg 抢写同一个文件
    with _get_job_lock(out):
        try:
            need = True
            if os.path.isfile(out):
                need = os.stat(out).st_mtime < os.stat(src).st_mtime or os.stat(out).st_mtime < os.stat(bak).st_mtime
            if need:
                proc = subprocess.run(
                    ['ffmpeg', '-y', '-loglevel', 'error',
                     '-i', src, '-i', bak,
                     '-map', '0:v:0', '-map', '1:a:0',
                     '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                     '-shortest', '-sn', out],
                    timeout=300)
                if proc.returncode != 0 or not os.path.isfile(out):
                    _dbg('backing-mv: ffmpeg 合成失败，退回纯伴奏音频')
                    return None
            _dbg('backing-mv: 使用合成档 %s' % os.path.basename(out))
            return out
        except Exception as e:
            _dbg('backing-mv: 例外 %s' % e)
            return None


def _delete_song_files(song):
    """删掉一首已播完的歌：主文件 + 伴奏文件 + 合成档 + 同名字幕，释放空间。"""
    if not song:
        return
    targets = [song.get('path'), song.get('backing')]
    base = os.path.splitext(song.get('path') or '')[0]
    targets += [base + ext for ext in SUBTITLE_EXTS]
    targets += [base + '_伴奏mv.mp4']
    for f in targets:
        if f and os.path.isfile(f):
            try:
                os.remove(f)
                _dbg('del: %s' % os.path.basename(f))
            except Exception:
                pass


def auto_advance():
    """自动推进线程：
    - 播放中且 mpv 完播退出（或暴毙）→ 自动切下一首 / 队列播完退出；
    - daemon(IPC 模式)存活时则轮询 idle-active 检测播完；
    - 定期检查磁盘用量，满了自动清旧歌/大档。
    """
    global player_proc
    tick = 0
    stall_pos = None      # 卡死偵測：最近一次觀察到的 pos
    stall_t0 = time.time()
    while auto_running:
        tick += 1
        try:
            was_playing = False
            with player_lock:
                was_playing = state['playing']
                alive = player_proc is not None and player_proc.poll() is None
                has_proc = player_proc is not None
            if not was_playing:
                # 暂停/停止期间进度本来就不动：卡死侦测的计时要归零，
                # 否则恢复播放时会被误判成「冻结过久」而整首重播
                stall_pos, stall_t0 = None, time.time()
                with player_lock:
                    idle_q = bool(state['queue'])
                    idle_song_ok = (state['queue']
                                    and 0 <= state['current_index'] < len(state['queue']))
                    idle_p = player_proc
                if idle_song_ok and not state['playing'] and not state['paused']:
                    play_current()  # 有歌单却没在播（意外停止/启动后）→ 恢复播放
                elif not idle_q and idle_p is not None and idle_p.poll() is None:
                    _kill_player()  # 歌单播空仍占用屏幕 → 退出 mpv，露出点歌界面
                time.sleep(0.3)
                continue
            # 播放中：song 结束（mpv 退出/暴毙）→ 切下一首；IPC daemon 仍活 → 轮询 idle-active
            ipc_alive = os.path.exists(MPV_SOCK)
            with player_lock:
                for_real = (state['playing'] and state['queue']
                            and 0 <= state['current_index'] < len(state['queue']))
            if not for_real:
                time.sleep(0.3)
                continue
            finished = False
            clean_exit = False
            elapsed = time.time() - spawn_ts
            # 当前播放歌曲时长（秒），用于“几乎播完”的容错判定
            cur_dur = None
            with player_lock:
                if state['queue'] and 0 <= state['current_index'] < len(state['queue']):
                    cur_dur = state['queue'][state['current_index']].get('duration')
            if was_playing and has_proc and not alive:
                rc = player_proc.returncode if player_proc is not None else None
                _dbg('auto: 检测到mpv退出 proc=%s rc=%s' % (player_proc.pid if player_proc else '无', rc))
                finished = True  # transient mpv 已退出 / daemon 暴毙
                near_end = (cur_dur and elapsed >= max(_MIN_PLAY_SECS, 0.85 * cur_dur))
                clean_exit = (rc == 0 and elapsed >= _MIN_PLAY_SECS) or (rc != 0 and near_end)
            elif ipc_alive:
                r = ipc_send(['get_property', 'idle-active'])
                if bool(r and r.get('data')):
                    finished = True
                    clean_exit = (elapsed >= _MIN_PLAY_SECS)  # daemon 空闲=歌曲自然播完
                else:
                    # 卡死偵測：mpv 活著但進度凍結（x11egl 偶發空轉，會空佔 CPU/RAM）
                    # 啟動後 12 秒起判；8 秒內 pos 未前進 0.6s 以上 → 判定卡死，自動重播該首
                    pr = ipc_send(['get_property', 'time-pos'])
                    pos = pr.get('data') if pr else None
                    if isinstance(pos, (int, float)) and pos >= 0 and elapsed >= 12:
                        if stall_pos is None:
                            stall_pos, stall_t0 = pos, time.time()
                        elif pos - stall_pos >= 0.6:
                            stall_pos, stall_t0 = pos, time.time()
                        elif time.time() - stall_t0 >= 8.0:
                            _dbg('auto: 侦测到播放卡死(pos=%.1f 冻结过久) → 重播' % pos)
                            with player_lock:
                                hung = None
                                if state['queue'] and 0 <= state['current_index'] < len(state['queue']):
                                    hung = state['queue'][state['current_index']]['path']
                                player_proc = None
                                state['playing'] = False
                            _kill_player()
                            stall_pos, stall_t0 = None, time.time()
                            if hung:
                                time.sleep(0.5)
                                play(hung)
                                time.sleep(0.3)
                            continue
                    else:
                        stall_pos, stall_t0 = None, time.time()
            if finished:
                if not clean_exit:
                    # mpv 异常结束（崩溃/被杀/文件打不开）：绝不删档，也不跳歌
                    _dbg('auto: mpv异常退出，保留文件、停止播放')
                    with player_lock:
                        player_proc = None
                        state['playing'] = False
                    _kill_player()
                    time.sleep(0.5)
                    continue
                next_file = None
                with player_lock:
                    if state['queue']:
                        pop_at = min(state['current_index'], len(state['queue']) - 1)
                        state['queue'].pop(pop_at)  # 已播完的那首移出播放清单（不删文件，歌单里还在）
                        state['current_index'] = min(pop_at, len(state['queue']) - 1)
                    cur = state['queue'][0] if state['queue'] else None
                    if cur:
                        state['current'] = cur
                        state['path_now'] = None
                        next_file = cur['path']
                    else:
                        state['playing'] = False
                        state['current'] = None
                        state['current_index'] = 0
                if next_file:
                    play(next_file)
                else:
                    _kill_player()  # 队列播完：彻底退出 mpv，露出点歌界面
        except Exception as e:
            with open('/tmp/jukebox-debug.log', 'a') as f:
                f.write('[auto] error: %r\n' % (e,))
        if tick % 200 == 0:  # 约每 1 分钟检查一次磁盘
            _maybe_cleanup()
        time.sleep(0.3)


def start_auto_adapter():
    global auto_thread
    if auto_thread is None or not auto_thread.is_alive():
        auto_thread = threading.Thread(target=auto_advance, daemon=True)
        auto_thread.start()


@app.route('/')
def index():
    """入口页：显示二维码，点“进入点歌”进入点歌页"""
    return render_template('lobby.html', url=get_qr_url())


@app.route('/dj')
def dj():
    """点歌页：多人排队点歌"""
    return render_template('index.html')


@app.route('/qr.png')
def qr_png():
    """生成二维码图片（扫描进入点歌页）"""
    try:
        import qrcode
        from qrcode.image.pil import PilImage
        url = request.args.get('url', get_qr_url())
        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color='#ffffff', back_color='#1e1e2f')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return Response(buf.getvalue(), mimetype='image/png',
                        headers={'Cache-Control': 'no-cache'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/songs')
def api_songs():
    """返回全部歌曲（可按关键词过滤，附帶played標記）"""
    q = request.args.get('q', '').lower()
    songs = find_songs()
    usage = _load_usage()
    played = {p for p, u in usage.items() if u.get('plays', 0) > 0}
    if q:
        songs = [s for s in songs if q in s['name'].lower() or q in s['artist'].lower()]
    for s in songs:
        s['played'] = s.get('path') in played
    return jsonify(songs)


_backing_yt_busy = False
_BACKING_YT = {}


def _yt_backing_worker(path):
    """背景：yt-dlp 搜尋「歌名 伴奏」，下載成純伴奏檔（歌名_伴奏.mp3）。"""
    global _backing_yt_busy
    try:
        base = os.path.splitext(path)[0]
        name0 = os.path.splitext(os.path.basename(path))[0].replace('-', ' ').replace('_', ' ').strip()
        query = (name0 + ' 伴奏').strip()
        dest = base + '_伴奏.mp3'
        _dbg('backing-yt: 搜尋「%s」' % query)
        proc = subprocess.Popen(
            [YTDLP, '-x', '--audio-format', 'mp3', '--audio-quality', '0', '--no-playlist',
             '--no-warnings', '-o', base + '_伴奏.%(ext)s', 'ytsearch1:' + query],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        rc = proc.wait(timeout=240)
        if rc != 0 or not os.path.isfile(dest):
            out = ''
            try:
                stdout, _ = proc.communicate()
                out = stdout[-400:]
            except Exception:
                pass
            _dbg('backing-yt: 失敗 rc=%s %s' % (rc, out))
            _BACKING_YT[path] = {'status': 'err', 'msg': '搜尋不到純伴奏（YouTube 可能限制）'}
            return
        _songs_cache['mtime'] = None
        _dbg('backing-yt: 完成 %s' % dest)
        if state['playing'] and state['current'] and state['mode'] == 'backing' and \
                os.path.splitext(state['current'].get('path', ''))[0] == base:
            try:
                play_current(new_mode='backing')
            except Exception:
                pass
        _BACKING_YT[path] = {'status': 'ok', 'msg': '已找到純伴奏', 'backing': dest}
    except Exception as e:
        _dbg('backing-yt: 例外 %s' % e)
        _BACKING_YT[path] = {'status': 'err', 'msg': str(e)}
    finally:
        _backing_yt_busy = False


@app.route('/api/find_backing')
def api_find_backing():
    """YouTube 搜尋並下載「歌名 伴奏」當純伴奏檔。"""
    global _backing_yt_busy
    path = _safe_songs_path((request.args.get('path') or '').strip())
    if not path or not os.path.isfile(path):
        return jsonify({'ok': False, 'error': '歌曲不存在'})
    existing = find_backing(path)
    if existing:
        return jsonify({'ok': True, 'backing': existing, 'note': '已有伴奏檔'})
    if _backing_yt_busy:
        return jsonify({'ok': False, 'error': '已有搜尋進行中，稍候再試'})
    if not os.path.isfile(YTDLP) and not shutil.which('yt-dlp'):
        return jsonify({'ok': False, 'error': '未安裝 yt-dlp'})
    _backing_yt_busy = True
    _BACKING_YT[path] = {'status': 'running'}
    threading.Thread(target=_yt_backing_worker, args=(path,), daemon=True).start()
    return jsonify({'ok': True, 'status': 'running'})


@app.route('/api/backing_status')
def api_backing_status():
    """查詢某首歌的伴奏搜尋/下載狀態。"""
    path = request.args.get('path', '')
    return jsonify({'self': _BACKING_YT.get(path, {'status': 'none'}),
                    'busy': _backing_yt_busy})


@app.route('/api/upload_backing', methods=['POST'])
def api_upload_backing():
    """上傳純伴奏檔（AI 去人聲的成品），存成 歌名_伴奏.<ext>。"""
    path = _safe_songs_path((request.form.get('path') or '').strip())
    f = request.files.get('file')
    if not path or not os.path.isfile(path):
        return jsonify({'ok': False, 'error': '歌曲不存在'})
    if f is None or not f.filename:
        return jsonify({'ok': False, 'error': '未收到檔案'})
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in _BACKING_EXTS:
        ext = '.mp3'
    base = os.path.splitext(path)[0]
    dest = base + '_伴奏' + ext
    f.save(dest)
    old = find_backing(path)
    if old and old != dest:
        try:
            os.remove(old)
        except Exception:
            pass
    _songs_cache['mtime'] = None
    _dbg('backing-upload: 已儲存 %s' % dest)
    if state['playing'] and state['current'] and state['mode'] == 'backing' and \
            os.path.splitext(state['current'].get('path', ''))[0] == base:
        try:
            play_current(new_mode='backing')
        except Exception:
            pass
    return jsonify({'ok': True, 'backing': dest})


@app.route('/api/play')
def api_play():
    """点歌：加入队列并立即播放"""
    raw_path = request.args.get('path', '')
    if not raw_path:
        return jsonify({'ok': False, 'error': '缺少 path'})
    path = _safe_songs_path(raw_path)
    if not path:
        return jsonify({'ok': False, 'error': '只能播放 songs 目录内的文件'})
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': '文件不存在'})
    name = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower()
    subtitle = find_subtitle(path)
    backing = find_backing(path)
    song = {'name': name, 'path': path,
            'artist': '未知', 'title': name,
            'is_video': ext in VIDEO_EXTS,
            'subtitle': subtitle,
            'backing': backing,
            'has_backing': bool(backing),
            'backing_source': any(k in name.lower() for k in (
                '原版伴奏', 'karaoke version', '纯伴奏', 'slow版伴奏')),
            'duration': _get_duration(path)}
    with player_lock:
        # 同一首歌已在清单里（正在播/等待中）就不重复加入，连点多次也只会有一首
        for i, q in enumerate(state['queue']):
            if q.get('path') == path:
                return jsonify({'ok': True, 'dup': True, 'index': i})
        state['queue'].append(song)
        idx = len(state['queue']) - 1
        start_now = not state['playing']
        if start_now:
            state['current_index'] = idx
    # 若当前没有真正在播放，立即播放这首新歌
    if start_now:
        play_current()
    return jsonify({'ok': True, 'index': idx})


@app.route('/api/playindex')
def api_playindex():
    """播放队列指定索引"""
    try:
        idx = int(request.args.get('index', -1))
    except ValueError:
        return jsonify({'ok': False, 'error': 'bad index'})
    if 0 <= idx < len(state['queue']):
        state['current_index'] = idx
        play_current()
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'index out of range'})


@app.route('/api/next')
def api_next():
    """下一首"""
    if state['queue'] and state['current_index'] < len(state['queue']) - 1:
        state['current_index'] += 1
        play_current()
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': '没有下一首'})


@app.route('/api/prev')
def api_prev():
    """上一首"""
    if state['queue'] and state['current_index'] > 0:
        state['current_index'] -= 1
        play_current()
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': '没有上一首'})


@app.route('/api/pause')
def api_pause():
    """暂停/恢复播放：IPC daemon 用 set_property pause；单次启动用 SIGSTOP/SIGCONT"""
    with player_lock:
        p = player_proc
        sock_ok = p is not None and p.poll() is None and os.path.exists(MPV_SOCK)
    if sock_ok:
        if state['playing']:
            ipc_send(['set_property', 'pause', True])
            state['playing'] = False
            state['paused'] = True
        else:
            ipc_send(['set_property', 'pause', False])
            state['playing'] = True
            state['paused'] = False
    elif p is not None and p.poll() is None:
        if state['playing']:
            try:
                os.kill(p.pid, signal.SIGSTOP)
            except OSError:
                pass
            state['playing'] = False
            state['paused'] = True
        else:
            try:
                os.kill(p.pid, signal.SIGCONT)
            except OSError:
                pass
            state['playing'] = True
            state['paused'] = False
    return jsonify({'ok': True, 'playing': state['playing']})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    stop_player()
    state['playing'] = False
    state['current'] = None
    state['current_index'] = 0
    state['queue'].clear()
    return jsonify({'ok': True})


@app.route('/api/queue')
def api_queue():
    """返回播放队列"""
    pos, dur = _mpv_time()
    usage = _load_usage()
    played = {p for p, u in usage.items() if u.get('plays', 0) > 0}
    result = []
    for i, s in enumerate(state['queue']):
        active = i == state['current_index']
        result.append({'index': i, 'path': s['path'],
                       'name': os.path.splitext(os.path.basename(s['path']))[0],
                       'active': active,
                       'playing': bool(active and state['playing']),
                       'pos': pos if active else None,
                       'dur': (dur if active and dur else s.get('duration')) or s.get('duration'),
                       'played': s.get('path') in played,
                       'has_backing': bool(s.get('backing')),
                       'backing_source': bool(s.get('backing_source'))})
    return jsonify(result)


@app.route('/api/seek')
def api_seek():
    """由手机拖曳進度條跳轉播放位置（秒）。"""
    try:
        pos = float(request.args.get('pos', 0))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'bad pos'})
    pos = max(0.0, pos)
    r = ipc_send(['set_property', 'time-pos', pos])
    if r is None or r.get('error') not in (None, 'success'):
        return jsonify({'ok': False, 'error': 'mpv 未就緒或無法跳轉'})
    return jsonify({'ok': True})


@app.route('/api/remove')
def api_remove():
    """把某首歌从播放清单移除（不删文件）。"""
    try:
        idx = int(request.args.get('index', -1))
    except ValueError:
        return jsonify({'ok': False, 'error': 'bad index'})
    with player_lock:
        if not (0 <= idx < len(state['queue'])):
            return jsonify({'ok': False, 'error': 'index out of range'})
        cur = state['queue'][state['current_index']] if state['queue'] else None
        if cur and cur.get('path') == state['queue'][idx].get('path'):
            removed_cur = True
        else:
            removed_cur = False
        state['queue'].pop(idx)
        state['current_index'] = max(0, min(state['current_index'], len(state['queue']) - 1 if state['queue'] else 0))
    if removed_cur:
        stop_player()
        state['current'] = None
    return jsonify({'ok': True})


@app.route('/api/state')
def api_state():
    cur = state['current']
    pos, dur = _mpv_time()
    total, used, free = _disk_usage()
    sizes = [s.get('size') or 0 for s in find_songs() if s.get('size')]
    avg = (sum(sizes) / len(sizes)) if sizes else 15 * 1024 ** 2
    est = int(free / avg) if avg > 0 else 0
    return jsonify({
        'playing': state['playing'],
        'volume': state['volume'],
        'current': state['current'],
        'queue_len': len(state['queue']),
        'current_index': state['current_index'],
        'mode': state['mode'],
        'has_subtitle': bool(cur and cur.get('subtitle')),
        'has_backing': bool(cur and cur.get('backing')),
        'backing_source': bool(cur and cur.get('backing_source')),
        'pos': pos,
        'dur': dur,
        'disk_total': total,
        'disk_used': used,
        'disk_free': free,
        'disk_percent': round(used * 100 / total, 1) if total else 0,
        'disk_est_songs': est,
    })


@app.route('/api/toggle_mode')
def api_toggle_mode():
    """切换当前歌曲的播放模式：导唱(人声)/伴奏"""
    m = request.args.get('mode', '')
    if m in ('lead', 'backing'):
        state['mode'] = m
    else:
        # 没有指定则反转
        state['mode'] = 'backing' if state['mode'] == 'lead' else 'lead'
    # 若正在播放，立即以新模式重播当前曲（保持同步位置近似，因常驻 mpv 几乎即时加载）
    if state['playing'] and state['current'] and 0 <= state['current_index'] < len(state['queue']):
        play_current(new_mode=state['mode'])
    return jsonify({'ok': True, 'mode': state['mode']})


@app.route('/api/volume')
def api_volume():
    """调节音量：实时生效（IPC 下发，无需重启歌曲）"""
    try:
        v = max(0, min(100, int(request.args.get('v', state['volume']))))
    except ValueError:
        v = state['volume']
    state['volume'] = v
    try:
        with open(VOLUME_FILE, 'w') as f:
            f.write(str(v))
    except Exception:
        pass
    ipc_send(['set_property', 'volume', v])
    return jsonify({'ok': True, 'volume': v})


@app.route('/api/delete', methods=['POST'])
def api_delete():
    """手动删除歌单里的一首歌：主文件 + 伴奏 + 字幕，并从播放队列中移除。"""
    raw_path = (request.form.get('path') or request.args.get('path') or '').strip()
    if not raw_path:
        return jsonify({'ok': False, 'error': '缺少 path'})
    path = _safe_songs_path(raw_path)
    if not path:
        return jsonify({'ok': False, 'error': '只能在 songs 目录内删除'})
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': '文件不存在'})
    if state.get('current') and state['current'].get('path') == path:
        stop_player()
    with player_lock:
        cur_path = state['current'].get('path') if state.get('current') else None
        state['queue'] = [s for s in state['queue'] if s.get('path') != path]
        if cur_path:
            idx = next((i for i, s in enumerate(state['queue']) if s.get('path') == cur_path), -1)
            state['current_index'] = idx if idx >= 0 else 0
        elif state['queue']:
            state['current_index'] = max(0, min(state['current_index'], len(state['queue']) - 1))
        else:
            state['current_index'] = 0
    _delete_song_files({'path': path, 'backing': find_backing(path)})
    try:
        u = _load_usage()
        u.pop(path, None)
        _save_usage(u)
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/songs/<path:filename>')
def serve_song(filename):
    return send_from_directory(SONGS_DIR, filename)


def parse_srt(path):
    """解析 SRT 字幕文件，返回 [{time, text}]（time 单位：秒，用于前端同步歌词）"""
    cues = []
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            content = f.read()
    except Exception:
        return cues
    # 按 cue 分块（空行分隔）
    blocks = re.split(r'\n\s*\n', content.strip())

    def to_sec(h, mi, s, ms):
        return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0

    for block in blocks:
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if len(lines) < 2:
            continue
        # 找出包含 --> 的时间行
        tline = next((i for i, l in enumerate(lines) if '-->' in l), -1)
        if tline < 0:
            continue
        m = re.search(r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)',
                      lines[tline])
        if not m:
            continue
        start = to_sec(m.group(1), m.group(2), m.group(3), m.group(4))
        end = to_sec(m.group(5), m.group(6), m.group(7), m.group(8))
        text = ' '.join(lines[tline + 1:])
        cues.append({'start': round(start, 3), 'end': round(end, 3), 'text': text})
    return cues


@app.route('/api/subtitle')
def api_subtitle():
    """返回指定歌曲的歌词（解析成 [{start,end,text}]，供手机同步显示）

    支持两种方式查歌词：
      - ?path= 本地文件路径
      - ?url=  相对 url（/songs/xxx.mp4），自动找同名字幕
    """
    p = request.args.get('path', '') or request.args.get('url', '')
    if not p and state['current'] and state['current'].get('subtitle'):
        p = state['current']['subtitle']
    if not p:
        return jsonify({'ok': False, 'error': '无歌词'}), 404
    if p.startswith('/songs/'):
        # url 形式 -> 转成磁盘路径再找同名字幕
        rel = p[len('/songs/'):]
        media = _safe_songs_path(os.path.join(SONGS_DIR, rel.replace('/', os.sep)))
        sub = find_subtitle(media) if media else None
    else:
        # 本地路径形式：一律先限制在 songs 目录内，避免读到目录外的任意文件
        safe_p = _safe_songs_path(p)
        if not safe_p:
            return jsonify({'ok': False, 'error': '无歌词'}), 404
        if os.path.isfile(safe_p) and os.path.splitext(safe_p)[1].lower() in SUBTITLE_EXTS:
            sub = safe_p
        else:
            # 视为媒体路径，找同名字幕
            sub = find_subtitle(safe_p)
    if not sub:
        return jsonify({'ok': False, 'error': '该歌曲无歌词'}), 404
    cues = parse_srt(sub)
    name = os.path.splitext(os.path.basename(sub))[0]
    return jsonify({'ok': True, 'lyrics': cues, 'name': name})



@app.route('/api/ytsearch')
def api_ytsearch():
    """用 yt-dlp 搜索 YouTube，把真正的歌曲排在最前（带类型标签）"""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'ok': False, 'error': '请输入搜索关键词'})
    try:
        proc = subprocess.run(
            [YTDLP, '--flat-playlist', '-J',
             'ytsearch10:' + q],
            capture_output=True, text=True, timeout=45,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            err = proc.stderr.strip().split('\n')[-1] if proc.stderr.strip() else '搜索失败'
            return jsonify({'ok': False, 'error': err})
        import json as j
        data = j.loads(proc.stdout)
        entries = data.get('entries', [])
        results = []
        for e in entries:
            vid = e.get('id')
            if not vid:
                continue
            title = e.get('title', '未知')
            dur = e.get('duration') or 0
            tag, score = _title_song_score(title, dur)
            results.append({
                'id': vid,
                'title': title,
                'artist': (e.get('uploader') or e.get('channel') or '').strip() or '未知',
                'duration': dur,
                'url': 'https://www.youtube.com/watch?v=' + vid,
                'tag': tag,
                'score': score,
            })
        results.sort(key=lambda r: r['score'], reverse=True)
        for r in results:
            r.pop('score', None)
        return jsonify({'ok': True, 'results': results})
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False, 'error': '搜索超时'})
    except FileNotFoundError:
        return jsonify({'ok': False, 'error': '未安装 yt-dlp'})
    except Exception:
        return jsonify({'ok': False, 'error': '搜索失败'})


def _title_song_score(title, dur):
    """按标题和时长估算「是不是正式歌曲」，并返回类型标签与排序分。"""
    t = (title or '').lower()
    song = ['official music video', 'official mv', 'official video',
            'official audio', 'original audio', 'official hd',
            'music video', 'mv', 'lyrics', 'karaoke', 'ktv', '字幕',
            'audio', 'official', '官方', '原唱']
    heavy_bad = ['live', '现场', '現場', '演唱会', '演唱會', 'concert',
                 'interview', '采访', '採訪', '访谈', '訪談',
                 '教学', '教學', 'tutorial', 'reaction', '电台', '電台']
    light_bad = ['cover', '翻唱', 'remix', '慢摇', 'dj版', '伴奏试听', '片段']
    score = 0.0
    for kw in song:
        if kw in t:
            score += 1.5
    for kw in heavy_bad:
        if kw in t:
            score -= 2.5
    for kw in light_bad:
        if kw in t:
            score -= 1.5
    if 120 <= dur <= 480:
        score += 1.0
    elif dur < 60 or dur > 900:
        score -= 1.0
    if 'official' in t or '官方' in t or 'mv' in t or 'audio' in t:
        tag = '歌曲'
    elif any(k in t for k in ('live', '现场', '現場', '演唱会', '演唱會', 'concert')):
        tag = '現場'
    elif any(k in t for k in ('cover', '翻唱')):
        tag = '翻唱'
    else:
        tag = '歌曲' if score >= 1.0 else '其它'
    return tag, score


def _download_worker(url):
    """后台下载：yt-dlp 输出进度行到日志文件，完成后入库并（可选）立即播放。"""
    dl_job.update({'busy': True, 'done': False, 'error': '', 'percent': 0.0,
                   'speed': '', 'eta': '', 'name': '', 'index': -1})
    os.makedirs(SONGS_DIR, exist_ok=True)
    logf = open(dl_job['log'], 'w', errors='replace')
    logf.close()
    # 先用 yt-dlp 获取标题
    title = '未知影片'
    try:
        info = subprocess.run([YTDLP, '--no-download', '--print', '%(title)s',
                               '--no-warnings', url],
                              capture_output=True, text=True, timeout=30)
        t = info.stdout.strip().split('\n')[0] if info.stdout.strip() else '未知影片'
        if t:
            title = t
    except Exception:
        pass
    output_template = os.path.join(SONGS_DIR, '%(title)s.%(ext)s')
    dl_cmd = [
        YTDLP,
        '-o', output_template,
        '--merge-output-format', 'mp4',
        '--concurrent-fragments', '16',
        '--newline', '--progress',
        '-f', ('bv*[vcodec^=avc1][height<=480]+ba'
               ' / bv*[vcodec^=h264][height<=480]+ba'
               ' / bv*[height<=480]+ba'
               ' / b[height<=480]'
               ' / b'),
        '--no-overwrites',
        '--no-warnings',
        url,
    ]
    proc = None
    try:
        with open(dl_job['log'], 'w', errors='replace') as lf:
            proc = subprocess.Popen(dl_cmd, stdout=lf, stderr=subprocess.STDOUT,
                                    text=True, errors='replace')
        proc.communicate(timeout=420)
        if proc.returncode != 0:
            err = 'yt-dlp 返回码 %s' % proc.returncode
            try:
                with open(dl_job['log'], 'r', errors='replace') as f:
                    for line in reversed(f.read().splitlines()):
                        if 'ERROR' in line or 'error' in line:
                            err = line.strip()
                            break
            except Exception:
                pass
            dl_job.update({'error': err, 'done': True, 'busy': False})
            return
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        dl_job.update({'error': '下载超时（超过7分钟）', 'done': True, 'busy': False})
        return
    except FileNotFoundError:
        dl_job.update({'error': '未安装 yt-dlp，请先运行 sudo apt install yt-dlp',
                       'done': True, 'busy': False})
        return
    # 第二阶段：单独拉字幕（失败不影响播放，避免被 YouTube 限速429弄死）
    try:
        sub_cmd = [
            YTDLP, '--skip-download',
            '--write-subs', '--write-auto-subs',
            '--sub-langs', 'zh.*,zh-Hant,zh-Hans,en,ja,ko',
            '--convert-subs', 'srt',
            '-o', output_template,
            '--no-overwrites', '--no-warnings',
            url,
        ]
        subprocess.run(sub_cmd, capture_output=True, text=True, timeout=120)
    except Exception:
        pass
    # 找到刚下载的文件（按修改时间排序取最新）
    candidates = []
    for f in os.listdir(SONGS_DIR):
        full = os.path.join(SONGS_DIR, f)
        if os.path.isfile(full) and not f.endswith('_伴奏' + os.path.splitext(f)[1]):
            ext = os.path.splitext(f)[1].lower()
            if ext in AUDIO_EXTS or ext in VIDEO_EXTS:
                candidates.append((os.path.getmtime(full), full))
    if not candidates:
        dl_job.update({'error': '下载完成但找不到影片文件', 'done': True, 'busy': False})
        return
    candidates.sort(reverse=True)
    path = candidates[0][1]
    _mark_usage(path, field='added')  # 记录下载时间，供自动清理判断新旧
    threading.Thread(target=_measure_loudness, args=(path,), daemon=True).start()
    name = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower()
    subtitle = find_subtitle(path)
    backing = find_backing(path)
    song = {
        'name': name, 'path': path,
        'artist': '网络', 'title': name,
        'is_video': ext in VIDEO_EXTS,
        'subtitle': subtitle,
        'backing': backing,
        'has_backing': bool(backing),
        'backing_source': any(k in name.lower() for k in (
            '原版伴奏', 'karaoke version', '纯伴奏', 'slow版伴奏')),
        'duration': _get_duration(path),
        'url': '/songs/' + os.path.relpath(path, SONGS_DIR).replace(os.sep, '/'),
    }
    state['queue'].append(song)
    idx = len(state['queue']) - 1
    if not state['playing']:
        state['current_index'] = idx
        play_current()
    elif dl_job['force_play']:
        state['current_index'] = idx
        play_current()
    dl_job.update({'name': name, 'index': idx, 'done': True, 'busy': False})


@app.route('/api/download_url', methods=['POST'])
def api_download_url():
    """开始后台下载（立即返回），进度走 /api/download_progress 轮询。"""
    data = request.get_json(silent=True) or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'ok': False, 'error': '请输入网址'})
    if dl_job['busy']:
        return jsonify({'ok': False, 'error': '已有下载进行中，请稍候'})
    force = bool(data.get('force'))
    dl_job['force_play'] = force
    threading.Thread(target=_download_worker, args=(url,), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/download_progress')
def api_download_progress():
    pct, speed, eta = 0.0, '', ''
    try:
        with open(dl_job['log'], 'r', errors='replace') as f:
            lines = f.read().splitlines()
        for line in reversed(lines):
            if '[download]' in line and '%' in line:
                m = re.search(r'(\d+(?:\.\d+)?)%', line)
                if m:
                    pct = float(m.group(1))
                sm = re.search(r'at\s+~?([\d.]+)\s*(\w+/s)', line)
                if sm:
                    speed = sm.group(1) + ' ' + sm.group(2)
                em = re.search(r'ETA\s+~?([0-9:]+)', line)
                if em:
                    eta = em.group(1)
                break
    except Exception:
        pass
    return jsonify({
        'busy': dl_job['busy'], 'done': dl_job['done'], 'error': dl_job['error'],
        'percent': pct, 'speed': speed, 'eta': eta,
        'name': dl_job['name'], 'index': dl_job['index'],
    })


@app.route('/api/media')
def api_media():
    """返回当前歌曲的媒体信息（视频/字幕路径）"""
    if state['current']:
        return jsonify({
            'ok': True,
            'name': state['current'].get('name', ''),
            'path': state['current'].get('path', ''),
            'is_video': state['current'].get('is_video', False),
            'subtitle': state['current'].get('subtitle'),
            'url': state['current'].get('url', ''),
        })
    return jsonify({'ok': False, 'error': '无播放中歌曲'}), 404


if __name__ == '__main__':
    # 启动自动播放队列线程
    start_auto_adapter()
    # 不预拉起常驻 mpv：闲置时屏幕属于点歌界面，首个播放时按需创建
    # 后台预热歌曲扫描缓存，避免用户首次打开点歌页时卡顿
    threading.Thread(target=lambda: (time.sleep(1), find_songs()), daemon=True).start()
    # 监听所有接口，手机能访问
    app.run(host='0.0.0.0', port=PORT, debug=False)
