#!/usr/bin/env python3
"""
多Agent协同工作流 — 本地代理服务器
解决浏览器直接请求通义万相API的兼容性问题
Usage: python3 server.py
然后浏览器打开 http://localhost:8888
云端部署可设置环境变量：HOST=0.0.0.0 PORT=8888
"""
import http.server
import json
import urllib.request
import urllib.error
import ssl
import os
import sys
import shutil
import hashlib
import subprocess
import time
import re
import base64
import logging
import glob
import sqlite3
import secrets
import hmac
import mimetypes
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta

HOST = os.environ.get('HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', '8888'))
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
MATERIAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'materials')
MATERIAL_INDEX = os.path.join(MATERIAL_DIR, 'index.json')
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
AUTH_DB = os.path.join(DATA_DIR, 'app.db')
APP_SETTINGS_FILE = os.path.join(DATA_DIR, 'app_settings.json')

DEFAULT_APP_SETTINGS = {
    'textProviderPreset': 'deepseek',
    'textUrl': 'https://api.deepseek.com/v1/chat/completions',
    'textKey': '',
    'textModel': 'deepseek-chat',
    'visionKey': '',
    'visionModel': 'qwen3.6-35b-a3b',
    'visionUrl': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
    'localStoryboardMode': False,
    'publicMaterialProvider': 'off',
    'publicMaterialPolicy': 'local_first',
    'wanxKey': '',
    'imgProvider': 'bailian',
    'imgModel': 'wanx-v1',
    'imgSize': '1024*1024',
    'i2vModel': 'wan2.7-i2v',
    'voice': 'longanyang',
    'ossEnabled': False,
    'ossAccessKeyId': '',
    'ossAccessKeySecret': '',
    'ossBucket': '',
    'ossRegion': '',
    'ossEndpoint': '',
    'ossPrefix': 'agent-workflow',
    'ossUrlExpires': '7200',
}

# 日志系统初始化
os.makedirs(LOG_DIR, exist_ok=True)
_server_logger = logging.getLogger('agent-workflow')
_server_logger.setLevel(logging.INFO)
# 按天轮转，保留7天
fh = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, 'server.log'),
    when='midnight', interval=1, backupCount=7, encoding='utf-8'
)
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
_server_logger.addHandler(fh)
# 启动时清理旧日志（兜底）
for old in sorted(glob.glob(os.path.join(LOG_DIR, 'server.log.*')), reverse=True)[7:]:
    try: os.remove(old)
    except: pass

def _log_cleanup():
    """清理7天以上的前端日志文件"""
    cutoff = time.time() - 7 * 86400
    for f in glob.glob(os.path.join(LOG_DIR, 'client_*.log')):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
        except:
            pass

def _db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(AUTH_DB)
    conn.row_factory = sqlite3.Row
    return conn

def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', str(password).encode('utf-8'), salt.encode('utf-8'), 120000).hex()
    return f'pbkdf2_sha256${salt}${digest}'

def _verify_password(password, stored):
    try:
        algo, salt, digest = str(stored or '').split('$', 2)
        if algo != 'pbkdf2_sha256':
            return False
        check = hashlib.pbkdf2_hmac('sha256', str(password).encode('utf-8'), salt.encode('utf-8'), 120000).hex()
        return secrets.compare_digest(check, digest)
    except Exception:
        return False

def _load_app_settings():
    os.makedirs(DATA_DIR, exist_ok=True)
    settings = dict(DEFAULT_APP_SETTINGS)
    try:
        with open(APP_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            settings.update({k: v for k, v in raw.items() if k in DEFAULT_APP_SETTINGS})
    except FileNotFoundError:
        pass
    except Exception as e:
        _server_logger.warning('读取系统配置失败: %s', e)
    if settings.get('i2vModel') == 'wan2.6-i2v':
        settings['i2vModel'] = 'wan2.7-i2v'
    settings.pop('vidModel', None)
    return settings

def _save_app_settings(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    current = _load_app_settings()
    clean = {}
    for key, default in DEFAULT_APP_SETTINGS.items():
        value = data.get(key, current.get(key, default))
        if isinstance(default, bool):
            clean[key] = bool(value)
        else:
            clean[key] = str(value or '').strip()
    if clean.get('i2vModel') == 'wan2.6-i2v':
        clean['i2vModel'] = 'wan2.7-i2v'
    with open(APP_SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean

def _init_auth_db():
    with _db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            nickname TEXT DEFAULT '',
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS quotas (
            user_id INTEGER PRIMARY KEY,
            total_credits INTEGER NOT NULL DEFAULT 0,
            used_credits INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS usage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            credits_used INTEGER NOT NULL DEFAULT 1,
            task_title TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        ''')
        admin = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
        if not admin:
            conn.execute(
                'INSERT INTO users(email,password_hash,nickname,role,status,notes) VALUES(?,?,?,?,?,?)',
                ('admin', _hash_password('admin123456'), '管理员', 'admin', 'active', '初始管理员，请尽快修改密码')
            )
            user_id = conn.execute("SELECT id FROM users WHERE email='admin'").fetchone()['id']
            conn.execute('INSERT OR IGNORE INTO quotas(user_id,total_credits,used_credits) VALUES(?,?,?)', (user_id, 999999, 0))

_init_auth_db()

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    """Serve index.html at root, proxy /api/* to external services."""

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._serve_html()
        elif self.path.startswith('/admin'):
            self._admin_page()
        elif self.path.startswith('/auth/me'):
            self._auth_me()
        elif self.path.startswith('/api/admin/users'):
            self._admin_users_list()
        elif self.path.startswith('/api/admin/usage'):
            self._admin_usage_list()
        elif self.path.startswith('/api/admin/settings'):
            self._admin_settings_get()
        elif self.path.startswith('/api/app-settings'):
            if not self._require_user():
                return
            self._app_settings_get()
        elif self.path.startswith('/oss/sign'):
            if not self._require_user():
                return
            self._oss_sign_endpoint()
        elif self.path.startswith('/image-base64'):
            if not self._require_user():
                return
            self._image_base64_get()
        elif self.path.startswith('/video-debug'):
            self._video_debug()
        elif self.path.startswith('/video-cache'):
            self._video_cache()
        elif self.path.startswith('/cached-video/'):
            self._cached_video_file()
        elif self.path.startswith('/material-file/'):
            self._material_file()
        elif self.path.startswith('/materials'):
            if not self._require_user():
                return
            self._materials_list()
        elif self.path.startswith('/asset-download'):
            self._asset_download()
        elif self.path.startswith('/video-proxy'):
            self._video_proxy()
        elif self.path.startswith('/publish/status'):
            if not self._require_user():
                return
            self._publish_status()
        elif self.path.startswith('/api/'):
            if not self._require_user():
                return
            self._proxy('GET')
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith('/auth/login'):
            self._auth_login()
        elif self.path.startswith('/auth/logout'):
            self._auth_logout()
        elif self.path.startswith('/usage/consume'):
            self._usage_consume()
        elif self.path.startswith('/api/admin/users'):
            self._admin_user_create()
        elif self.path.startswith('/api/admin/quota'):
            self._admin_quota_update()
        elif self.path.startswith('/api/admin/status'):
            self._admin_status_update()
        elif self.path.startswith('/api/admin/password'):
            self._admin_password_update()
        elif self.path.startswith('/api/admin/settings'):
            self._admin_settings_update()
        elif self.path.startswith('/voice-compose'):
            if not self._require_user():
                return
            self._voice_compose()
        elif self.path.startswith('/tts-generate'):
            if not self._require_user():
                return
            self._tts_generate()
        elif self.path.startswith('/video-stitch'):
            if not self._require_user():
                return
            self._video_stitch()
        elif self.path.startswith('/kenburns-compose'):
            if not self._require_user():
                return
            self._kenburns_compose()
        elif self.path.startswith('/pexels-compose'):
            if not self._require_user():
                return
            self._pexels_compose()
        elif self.path.startswith('/material-upload'):
            if not self._require_user():
                return
            self._material_upload()
        elif self.path.startswith('/material-import-image'):
            if not self._require_user():
                return
            self._material_import_image()
        elif self.path.startswith('/material-update'):
            if not self._require_user():
                return
            self._material_update()
        elif self.path.startswith('/material-delete'):
            if not self._require_user():
                return
            self._material_delete()
        elif self.path.startswith('/log'):
            self._log()
        elif self.path.startswith('/publish/douyin-draft'):
            if not self._require_user():
                return
            self._publish_douyin_draft()
        elif self.path.startswith('/mix-compose'):
            if not self._require_user():
                return
            self._mix_compose()
        elif self.path.startswith('/api/'):
            if not self._require_user():
                return
            self._proxy('POST')
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _video_proxy(self):
        """Proxy video content from restricted URL."""
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        video_url = query.get('url', [None])[0]
        if not video_url:
            self.send_error(400, 'Missing url parameter')
            return
        try:
            cache_path = self._cache_video(video_url, self._video_auth_header())
            self._serve_cached_video(cache_path)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self._cors_headers()
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_error(502, f'Video proxy error: {str(e)}')

    def _video_debug(self):
        """Return a small JSON diagnosis for a remote video URL."""
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        video_url = query.get('url', [None])[0]
        if not video_url:
            self.send_error(400, 'Missing url parameter')
            return
        try:
            cache_path = self._cache_video(video_url, self._video_auth_header())
            size = os.path.getsize(cache_path)
            with open(cache_path, 'rb') as f:
                head = f.read(64)
            payload = {
                'ok': True,
                'size': size,
                'path': cache_path,
                'headHex': head[:24].hex(),
                'looksMp4': self._looks_like_mp4(head),
            }
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self._cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            payload = {'ok': False, 'error': str(e)}
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(502)
            self._cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _video_cache(self):
        """Cache a remote video and return a stable local playback URL."""
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        video_url = query.get('url', [None])[0]
        if not video_url:
            self.send_error(400, 'Missing url parameter')
            return
        try:
            cache_path = self._cache_video(video_url, self._video_auth_header())
            filename = os.path.basename(cache_path)
            try:
                import imageio_ffmpeg
                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg_exe = shutil.which('ffmpeg')
            duration = self._media_duration(ffmpeg_exe, cache_path) if ffmpeg_exe else None
            has_audio = self._media_has_audio(ffmpeg_exe, cache_path) if ffmpeg_exe else None
            width, height = self._media_dimensions(ffmpeg_exe, cache_path) if ffmpeg_exe else (None, None)
            payload = {
                'ok': True,
                'url': f'/cached-video/{filename}',
                'size': os.path.getsize(cache_path),
                'duration': duration,
                'hasAudio': has_audio,
                'width': width,
                'height': height,
            }
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self._cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            payload = {'ok': False, 'error': str(e)}
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(502)
            self._cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _cached_video_file(self):
        """Serve a previously cached local video by filename."""
        from urllib.parse import urlparse, unquote
        filename = os.path.basename(unquote(urlparse(self.path).path))
        if not filename.lower().endswith(('.mp4', '.mp3', '.aiff')) or '/' in filename or '\\' in filename:
            self.send_error(400, 'Invalid cached media filename')
            return
        cache_path = os.path.join(self._video_cache_dir(), filename)
        if not os.path.exists(cache_path):
            self.send_error(404, 'Cached video not found')
            return
        self._serve_cached_video(cache_path)

    def _asset_download(self):
        """Download a remote image/video asset through localhost."""
        from urllib.parse import urlparse, parse_qs, unquote
        query = parse_qs(urlparse(self.path).query)
        asset_url = query.get('url', [None])[0]
        filename = os.path.basename(unquote(query.get('name', ['asset'])[0])) or 'asset'
        if not asset_url:
            self.send_error(400, 'Missing url parameter')
            return
        try:
            ctx = ssl._create_unverified_context()
            req = urllib.request.Request(asset_url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Accept': 'image/*,video/*,*/*;q=0.8',
            })
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                content = resp.read()
                content_type = resp.headers.get('Content-Type', 'application/octet-stream')
            self.send_response(200)
            self._cors_headers()
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(502, f'Asset download error: {str(e)}')

    def _video_cache_dir(self):
        cache_dir = os.path.join('/tmp', 'agent_workflow_video_cache')
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir

    def _oss_config(self):
        settings = _load_app_settings()
        enabled = bool(settings.get('ossEnabled'))
        endpoint = str(settings.get('ossEndpoint') or '').strip().replace('https://', '').replace('http://', '').rstrip('/')
        bucket = str(settings.get('ossBucket') or '').strip()
        access_key_id = str(settings.get('ossAccessKeyId') or '').strip()
        access_key_secret = str(settings.get('ossAccessKeySecret') or '').strip()
        if not enabled or not endpoint or not bucket or not access_key_id or not access_key_secret:
            return None
        try:
            expires = max(300, min(86400, int(settings.get('ossUrlExpires') or 7200)))
        except Exception:
            expires = 7200
        prefix = str(settings.get('ossPrefix') or 'agent-workflow').strip().strip('/') or 'agent-workflow'
        return {'endpoint': endpoint, 'bucket': bucket, 'accessKeyId': access_key_id, 'accessKeySecret': access_key_secret, 'prefix': prefix, 'expires': expires}

    def _oss_host(self, cfg):
        endpoint = cfg['endpoint']
        bucket = cfg['bucket']
        return endpoint if endpoint.startswith(bucket + '.') else f'{bucket}.{endpoint}'

    def _oss_sign(self, cfg, method, object_key, expires_or_date, content_type='', content_md5=''):
        canonical = f"/{cfg['bucket']}/{object_key}"
        string_to_sign = f"{method}\n{content_md5}\n{content_type}\n{expires_or_date}\n{canonical}"
        digest = hmac.new(cfg['accessKeySecret'].encode('utf-8'), string_to_sign.encode('utf-8'), hashlib.sha1).digest()
        return base64.b64encode(digest).decode('utf-8')

    def _oss_signed_url(self, object_key, cfg=None):
        cfg = cfg or self._oss_config()
        if not cfg or not object_key:
            return ''
        from urllib.parse import quote
        expires = int(time.time()) + int(cfg['expires'])
        signature = self._oss_sign(cfg, 'GET', object_key, str(expires))
        return f"https://{self._oss_host(cfg)}/{quote(object_key)}?OSSAccessKeyId={quote(cfg['accessKeyId'])}&Expires={expires}&Signature={quote(signature)}"

    def _oss_upload_file(self, path, category='materials'):
        cfg = self._oss_config()
        if not cfg or not path or not os.path.exists(path):
            return {}
        ext = os.path.splitext(path)[1].lower()
        digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()[:24]
        object_key = f"{cfg['prefix']}/{category}/{datetime.now().strftime('%Y/%m/%d')}/{int(time.time())}_{digest}{ext}"
        content_type = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        date = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
        signature = self._oss_sign(cfg, 'PUT', object_key, date, content_type=content_type)
        url = f"https://{self._oss_host(cfg)}/{object_key}"
        with open(path, 'rb') as f:
            payload = f.read()
        req = urllib.request.Request(url, data=payload, headers={
            'Date': date,
            'Content-Type': content_type,
            'Authorization': f"OSS {cfg['accessKeyId']}:{signature}",
        }, method='PUT')
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx, timeout=180) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f'OSS 上传失败 HTTP {resp.status}')
        return {'ossKey': object_key, 'cloudUrl': self._oss_signed_url(object_key, cfg=cfg)}

    def _refresh_material_cloud_urls(self, item):
        if isinstance(item, dict) and item.get('ossKey'):
            signed = self._oss_signed_url(item.get('ossKey'))
            if signed:
                item['cloudUrl'] = signed
        return item

    def _material_dir(self):
        os.makedirs(MATERIAL_DIR, exist_ok=True)
        return MATERIAL_DIR

    def _load_material_index(self):
        self._material_dir()
        if not os.path.exists(MATERIAL_INDEX):
            return []
        try:
            with open(MATERIAL_INDEX, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_material_index(self, items):
        self._material_dir()
        with open(MATERIAL_INDEX, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

    def _extract_image_metadata(self, path):
        """Extract cheap local image metadata without calling a vision model."""
        try:
            from PIL import Image, ImageStat
        except Exception:
            return {}

        try:
            with Image.open(path) as img:
                width, height = img.size
                if not width or not height:
                    return {}

                ratio = round(width / height, 4)
                orientation = '方图'
                if ratio > 1.08:
                    orientation = '横图'
                elif ratio < 0.92:
                    orientation = '竖图'

                tags = [orientation]
                if abs(ratio - (9 / 16)) <= 0.04:
                    tags.append('9:16')
                elif abs(ratio - (16 / 9)) <= 0.08:
                    tags.append('16:9')
                elif abs(ratio - 1) <= 0.04:
                    tags.append('1:1')
                elif abs(ratio - (3 / 4)) <= 0.04:
                    tags.append('3:4')
                elif abs(ratio - (4 / 3)) <= 0.06:
                    tags.append('4:3')

                sample = img.convert('RGB').resize((1, 1))
                r, g, b = [int(v) for v in ImageStat.Stat(sample).mean[:3]]
                dominant_color = f'#{r:02x}{g:02x}{b:02x}'

                return {
                    'dimensions': {'width': width, 'height': height},
                    'aspectRatio': ratio,
                    'orientation': orientation,
                    'dominantColor': dominant_color,
                    'format': img.format or '',
                    'mode': img.mode or '',
                    'tags': tags,
                }
        except Exception:
            return {}

    def _add_image_to_materials(self, source_path, image_url='', filename='', tags=None):
        """自动将AI生图加入素材库，避免额度浪费"""
        try:
            digest = hashlib.sha256(open(source_path, 'rb').read()).hexdigest()[:16]
            ext = os.path.splitext(source_path)[1].lower() or '.png'
            timestamp = int(time.time())
            safe_name = f'{timestamp}_{digest}{ext}'
            dest_path = os.path.join(self._material_dir(), safe_name)
            items = self._load_material_index()
            existing = next((i for i in items if i.get('id') == digest), None)
            if existing:
                return existing
            shutil.copy2(source_path, dest_path)
            size = os.path.getsize(dest_path)
            metadata = self._extract_image_metadata(dest_path)
            auto_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
            merged_tags = ['AI生成', '自动入库']
            if tags:
                merged_tags = list(dict.fromkeys([*merged_tags, *auto_tags]))
            if metadata.get('tags'):
                merged_tags = list(dict.fromkeys([*merged_tags, *metadata.get('tags')]))
            item = {
                'id': digest,
                'filename': filename or f'AI生图_{timestamp}',
                'storedName': safe_name,
                'type': f'image/{ext[1:]}',
                'kind': 'image',
                'tags': merged_tags,
                'manualTags': [],
                'aiTags': list(dict.fromkeys(merged_tags)),
                'size': size,
                'createdAt': timestamp,
                'url': f'/material-file/{safe_name}',
            }
            try:
                item.update(self._oss_upload_file(dest_path, category='materials'))
            except Exception as oss_error:
                item['ossError'] = str(oss_error)[:500]
            if image_url:
                item['sourceUrl'] = image_url
            if metadata:
                item['metadata'] = metadata
                item['dimensions'] = metadata.get('dimensions')
                item['aspectRatio'] = metadata.get('aspectRatio')
                item['orientation'] = metadata.get('orientation')
                item['dominantColor'] = metadata.get('dominantColor')
            items.insert(0, item)
            self._save_material_index(items[:200])
            return item
        except Exception:
            return None

    def _log(self):
        """接收前端日志并写入文件"""
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            entries = data if isinstance(data, list) else [data]
            today = datetime.now().strftime('%Y-%m-%d')
            log_path = os.path.join(LOG_DIR, f'client_{today}.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                for e in entries:
                    level = e.get('level', 'INFO')
                    scope = e.get('scope', 'client')
                    message = e.get('message', '')
                    f.write(f'{datetime.now().strftime("%H:%M:%S")} [{level}] [{scope}] {message}\n')
            _log_cleanup()
            self._json_response(200, {'ok': True})
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)[:200]})

    def _materials_list(self):
        items = self._load_material_index()
        changed = False
        for item in items:
            if not item.get('ossKey') and self._oss_config():
                stored = os.path.basename(str(item.get('storedName') or ''))
                path = os.path.join(self._material_dir(), stored)
                if stored and os.path.exists(path):
                    try:
                        item.update(self._oss_upload_file(path, category='materials'))
                        changed = True
                    except Exception as oss_error:
                        item['ossError'] = str(oss_error)[:500]
                        changed = True
            self._refresh_material_cloud_urls(item)
        if changed:
            self._save_material_index(items[:200])
        self._json_response(200, {'ok': True, 'items': items})

    def _material_upload(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            filename = os.path.basename((data.get('filename') or 'material').replace('\\', '/'))
            mime = (data.get('type') or 'application/octet-stream').strip()
            tags = data.get('tags') or []
            if isinstance(tags, str):
                tags = [t.strip() for t in re.split(r'[,，\s]+', tags) if t.strip()]
            kind = 'video' if mime.startswith('video/') else 'image' if mime.startswith('image/') else 'file'
            data_url = data.get('dataUrl') or ''
            if ',' not in data_url:
                raise ValueError('上传数据格式错误')
            payload = data_url.split(',', 1)[1]
            content = base64.b64decode(payload)
            if len(content) < 32:
                raise ValueError('上传文件为空')
            ext = os.path.splitext(filename)[1].lower()
            if not ext:
                ext = '.mp4' if kind == 'video' else '.png' if kind == 'image' else '.bin'
            digest = hashlib.sha256(content).hexdigest()[:16]
            safe_name = f'{int(time.time())}_{digest}{ext}'
            path = os.path.join(self._material_dir(), safe_name)
            with open(path, 'wb') as f:
                f.write(content)
            item = {
                'id': digest,
                'filename': filename,
                'storedName': safe_name,
                'type': mime,
                'kind': kind,
                'tags': tags,
                'manualTags': tags,
                'aiTags': [],
                'size': len(content),
                'createdAt': int(time.time()),
                'url': f'/material-file/{safe_name}',
            }
            try:
                item.update(self._oss_upload_file(path, category='materials'))
            except Exception as oss_error:
                item['ossError'] = str(oss_error)[:500]
            vision_key = (data.get('visionKey') or '').strip()
            if vision_key:
                try:
                    item['analysis'] = self._analyze_material_with_vision(
                        path,
                        kind,
                        vision_key,
                        (data.get('visionModel') or 'qwen3.6-35b-a3b').strip(),
                        (data.get('visionUrl') or 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions').strip(),
                    )
                    auto_tags = item.get('analysis', {}).get('autoTags') or []
                    item['aiTags'] = auto_tags
                    item['tags'] = list(dict.fromkeys([*tags, *auto_tags]))
                except Exception as analysis_error:
                    item['analysisError'] = str(analysis_error)[:500]
            items = [x for x in self._load_material_index() if x.get('storedName') != safe_name]
            items.insert(0, item)
            self._save_material_index(items[:200])
            self._json_response(200, {'ok': True, 'item': item})
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _material_import_image(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            image_url = (data.get('url') or data.get('imageUrl') or '').strip()
            if not image_url:
                raise ValueError('缺少图片 URL')
            filename = os.path.basename((data.get('filename') or '').replace('\\', '/'))
            tags = data.get('tags') or []
            if isinstance(tags, str):
                tags = [t.strip() for t in re.split(r'[,，\s]+', tags) if t.strip()]
            elif not isinstance(tags, list):
                tags = []

            from urllib.parse import urlparse, unquote
            parsed = urlparse(image_url)
            temp_path = None
            if parsed.scheme == 'data':
                if ',' not in image_url:
                    raise ValueError('data URL 格式错误')
                header, payload = image_url.split(',', 1)
                ext = '.jpg' if 'jpeg' in header else '.webp' if 'webp' in header else '.png'
                temp_path = os.path.join(self._video_cache_dir(), f'import_image_{hashlib.sha256(payload[:200].encode()).hexdigest()[:16]}{ext}')
                with open(temp_path, 'wb') as f:
                    f.write(base64.b64decode(payload))
            elif parsed.path.startswith('/material-file/'):
                stored = os.path.basename(unquote(parsed.path))
                existing = next((i for i in self._load_material_index() if i.get('storedName') == stored), None)
                if existing:
                    self._json_response(200, {'ok': True, 'item': existing, 'deduped': True})
                    return
                temp_path = os.path.join(self._material_dir(), stored)
            elif parsed.scheme in ('http', 'https'):
                cache_dir = self._video_cache_dir()
                key = hashlib.sha256(image_url.encode('utf-8')).hexdigest()[:16]
                ext = os.path.splitext(parsed.path)[1].lower()
                if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
                    ext = '.jpg'
                temp_path = os.path.join(cache_dir, f'import_image_{key}{ext}')
                if not os.path.exists(temp_path) or os.path.getsize(temp_path) < 1024:
                    ctx = ssl._create_unverified_context()
                    req = urllib.request.Request(image_url, headers={
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                        'Accept': 'image/*,*/*;q=0.8',
                    })
                    with urllib.request.urlopen(req, context=ctx, timeout=120) as resp, open(temp_path, 'wb') as f:
                        shutil.copyfileobj(resp, f)
            else:
                raise ValueError('暂不支持的图片地址')

            if not temp_path or not os.path.exists(temp_path) or os.path.getsize(temp_path) < 1024:
                raise RuntimeError('图片下载失败或文件过小')
            item = self._add_image_to_materials(temp_path, image_url, filename=filename, tags=tags)
            if not item:
                raise RuntimeError('图片入库失败')
            self._json_response(200, {'ok': True, 'item': item})
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _image_base64_get(self):
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            image_url = (qs.get('url') or [''])[0].strip()
            if not image_url:
                raise ValueError('缺少图片 URL')
            path = self._resolve_image_path(image_url)
            if not path or not os.path.exists(path):
                raise RuntimeError('图片读取失败')
            size = os.path.getsize(path)
            if size <= 0:
                raise RuntimeError('图片为空')
            if size > 8 * 1024 * 1024:
                raise RuntimeError('图片过大，无法转为动态图生视频输入')
            ext = os.path.splitext(path)[1].lower()
            mime = 'image/jpeg'
            if ext == '.png':
                mime = 'image/png'
            elif ext == '.webp':
                mime = 'image/webp'
            with open(path, 'rb') as f:
                payload = base64.b64encode(f.read()).decode('ascii')
            self._json_response(200, {'ok': True, 'mime': mime, 'base64': payload, 'size': size})
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _material_update(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            stored = os.path.basename(str(data.get('storedName') or ''))
            if not stored:
                raise ValueError('缺少素材文件名')
            manual_tags = data.get('manualTags', data.get('tags', []))
            if isinstance(manual_tags, str):
                manual_tags = [t.strip() for t in re.split(r'[,，\s]+', manual_tags) if t.strip()]
            elif isinstance(manual_tags, list):
                manual_tags = [str(t).strip() for t in manual_tags if str(t).strip()]
            else:
                manual_tags = []
            note = str(data.get('note') or '')[:500]
            items = self._load_material_index()
            updated = None
            for item in items:
                if item.get('storedName') == stored:
                    ai_tags = item.get('aiTags') or []
                    if not ai_tags and isinstance(item.get('analysis'), dict):
                        ai_tags = item.get('analysis', {}).get('autoTags') or []
                    item['manualTags'] = list(dict.fromkeys(manual_tags))
                    item['aiTags'] = list(dict.fromkeys([str(t).strip() for t in ai_tags if str(t).strip()]))
                    item['tags'] = list(dict.fromkeys([*item['manualTags'], *item['aiTags']]))
                    item['note'] = note
                    item['updatedAt'] = int(time.time())
                    updated = item
                    break
            if not updated:
                raise FileNotFoundError('素材不存在')
            self._save_material_index(items)
            self._json_response(200, {'ok': True, 'item': updated})
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _material_delete(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            stored = os.path.basename(str(data.get('storedName') or ''))
            if not stored:
                raise ValueError('缺少素材文件名')
            items = self._load_material_index()
            next_items = [x for x in items if x.get('storedName') != stored]
            path = os.path.join(self._material_dir(), stored)
            if os.path.exists(path):
                os.remove(path)
            self._save_material_index(next_items)
            self._json_response(200, {'ok': True})
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _material_file(self):
        from urllib.parse import urlparse, unquote
        filename = os.path.basename(unquote(urlparse(self.path).path))
        path = os.path.join(self._material_dir(), filename)
        if not os.path.exists(path):
            self.send_error(404, 'Material not found')
            return
        content_type = 'video/mp4' if filename.lower().endswith(('.mp4', '.mov', '.webm')) else 'image/png'
        self.send_response(200)
        self._cors_headers()
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(os.path.getsize(path)))
        self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()
        with open(path, 'rb') as f:
            shutil.copyfileobj(f, self.wfile, length=1024 * 512)

    def _analyze_material_with_vision(self, path, kind, api_key, model, api_url):
        images = [path] if kind == 'image' else self._extract_video_frames(path, max_frames=3)
        if not images:
            raise RuntimeError('没有可识别的图片帧')
        content = [{
            'type': 'text',
            'text': (
                '请识别这个企业短视频素材，输出严格JSON，不要Markdown。'
                '字段：autoTags(中文标签数组，5-10个), sceneDesc(一句话场景描述), '
                'bestUse(适合用在短视频哪个镜头), quality(清晰/一般/较差), '
                'orientation(横屏/竖屏/方形), risks(风险数组，如水印、Logo、外国人正脸、文字乱码、敏感内容；没有则空数组)。'
            ),
        }]
        for image_path in images:
            content.append({
                'type': 'image_url',
                'image_url': {'url': self._image_file_to_data_url(image_path)},
            })
        payload = {
            'model': model,
            'messages': [{'role': 'user', 'content': content}],
            'temperature': 0.2,
        }
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'AgentWorkflow/1.0',
            },
            method='POST',
        )
        ctx = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'素材视觉识别失败 HTTP {e.code}: {detail[:500]}')
        text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
        parsed = self._parse_json_from_text(text)
        return {
            'autoTags': [str(x).strip() for x in parsed.get('autoTags', []) if str(x).strip()][:12],
            'sceneDesc': str(parsed.get('sceneDesc') or '').strip(),
            'bestUse': str(parsed.get('bestUse') or '').strip(),
            'quality': str(parsed.get('quality') or '').strip(),
            'orientation': str(parsed.get('orientation') or '').strip(),
            'risks': [str(x).strip() for x in parsed.get('risks', []) if str(x).strip()][:10],
            'model': model,
        }

    def _extract_video_frames(self, path, max_frames=3):
        try:
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg_exe = shutil.which('ffmpeg')
        if not ffmpeg_exe:
            raise RuntimeError('未找到 ffmpeg，无法抽取视频关键帧')
        duration = self._media_duration(ffmpeg_exe, path) or 0
        points = [0.8]
        if duration > 2:
            points.append(max(0.5, duration / 2))
        if duration > 4:
            points.append(max(0.5, duration - 1))
        frame_paths = []
        cache_dir = self._video_cache_dir()
        key = hashlib.sha256((path + str(os.path.getmtime(path))).encode('utf-8')).hexdigest()[:12]
        for idx, second in enumerate(points[:max_frames]):
            frame_path = os.path.join(cache_dir, f'material_frame_{key}_{idx}.jpg')
            cmd = [ffmpeg_exe, '-y', '-ss', f'{second:.2f}', '-i', path, '-frames:v', '1', '-q:v', '3', frame_path]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode == 0 and os.path.exists(frame_path) and os.path.getsize(frame_path) > 512:
                frame_paths.append(frame_path)
        return frame_paths

    def _image_file_to_data_url(self, path):
        ext = os.path.splitext(path)[1].lower()
        mime = 'image/png'
        if ext in ('.jpg', '.jpeg'):
            mime = 'image/jpeg'
        elif ext == '.webp':
            mime = 'image/webp'
        with open(path, 'rb') as f:
            encoded = base64.b64encode(f.read()).decode('ascii')
        return f'data:{mime};base64,{encoded}'

    def _parse_json_from_text(self, text):
        cleaned = str(text or '').strip()
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
        try:
            return json.loads(cleaned)
        except Exception:
            match = re.search(r'\{.*\}', cleaned, re.S)
            if match:
                return json.loads(match.group(0))
            raise RuntimeError('视觉模型返回不是JSON: ' + cleaned[:500])

    def _json_response(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        return json.loads(raw.decode('utf-8') or '{}')

    def _auth_token(self):
        auth = self.headers.get('Authorization') or ''
        if auth.lower().startswith('bearer '):
            return auth.split(' ', 1)[1].strip()
        return (self.headers.get('X-Auth-Token') or '').strip()

    def _current_user(self):
        token = self._auth_token()
        if not token:
            return None
        now = datetime.utcnow().isoformat()
        with _db() as conn:
            row = conn.execute('''
                SELECT u.*, q.total_credits, q.used_credits
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                LEFT JOIN quotas q ON q.user_id = u.id
                WHERE s.token = ? AND s.expires_at > ?
            ''', (token, now)).fetchone()
        return row

    def _user_payload(self, row):
        total = int(row['total_credits'] or 0)
        used = int(row['used_credits'] or 0)
        return {
            'id': row['id'],
            'email': row['email'],
            'nickname': row['nickname'] or '',
            'role': row['role'],
            'status': row['status'],
            'totalCredits': total,
            'usedCredits': used,
            'remainingCredits': max(0, total - used),
        }

    def _require_user(self):
        user = self._current_user()
        if not user or user['status'] != 'active':
            self._json_response(401, {'ok': False, 'error': '请先登录，或账号已被禁用'})
            return None
        return user

    def _require_admin(self):
        user = self._require_user()
        if not user:
            return None
        if user['role'] != 'admin':
            self._json_response(403, {'ok': False, 'error': '需要管理员权限'})
            return None
        return user

    def _auth_login(self):
        try:
            data = self._read_json_body()
            email = str(data.get('email') or '').strip()
            password = str(data.get('password') or '')
            if not email or not password:
                raise ValueError('请输入账号和密码')
            with _db() as conn:
                row = conn.execute('''
                    SELECT u.*, q.total_credits, q.used_credits
                    FROM users u LEFT JOIN quotas q ON q.user_id = u.id
                    WHERE u.email = ?
                ''', (email,)).fetchone()
                if not row or row['status'] != 'active' or not _verify_password(password, row['password_hash']):
                    self._json_response(401, {'ok': False, 'error': '账号或密码错误，或账号已停用'})
                    return
                token = secrets.token_urlsafe(32)
                expires_at = (datetime.utcnow() + timedelta(days=14)).isoformat()
                conn.execute('INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)', (token, row['id'], expires_at))
            self._json_response(200, {'ok': True, 'token': token, 'user': self._user_payload(row)})
        except Exception as e:
            self._json_response(400, {'ok': False, 'error': str(e)})

    def _auth_logout(self):
        token = self._auth_token()
        if token:
            with _db() as conn:
                conn.execute('DELETE FROM sessions WHERE token = ?', (token,))
        self._json_response(200, {'ok': True})

    def _auth_me(self):
        user = self._require_user()
        if not user:
            return
        self._json_response(200, {'ok': True, 'user': self._user_payload(user)})

    def _usage_consume(self):
        user = self._require_user()
        if not user:
            return
        try:
            data = self._read_json_body()
            credits = max(1, int(data.get('credits') or 1))
            action = str(data.get('action') or 'generate_video')[:80]
            task_title = str(data.get('taskTitle') or '')[:200]
            with _db() as conn:
                quota = conn.execute('SELECT total_credits, used_credits FROM quotas WHERE user_id = ?', (user['id'],)).fetchone()
                total = int(quota['total_credits'] if quota else 0)
                used = int(quota['used_credits'] if quota else 0)
                if total - used < credits:
                    self._json_response(402, {'ok': False, 'error': '免费次数已用完，请联系管理员开通', 'remainingCredits': max(0, total - used)})
                    return
                conn.execute('INSERT OR IGNORE INTO quotas(user_id,total_credits,used_credits) VALUES(?,?,?)', (user['id'], total, used))
                conn.execute('UPDATE quotas SET used_credits = used_credits + ? WHERE user_id = ?', (credits, user['id']))
                conn.execute('INSERT INTO usage_records(user_id,action,credits_used,task_title) VALUES(?,?,?,?)', (user['id'], action, credits, task_title))
                new_quota = conn.execute('SELECT total_credits, used_credits FROM quotas WHERE user_id = ?', (user['id'],)).fetchone()
            remaining = max(0, int(new_quota['total_credits']) - int(new_quota['used_credits']))
            self._json_response(200, {'ok': True, 'remainingCredits': remaining, 'usedCredits': int(new_quota['used_credits'])})
        except Exception as e:
            self._json_response(400, {'ok': False, 'error': str(e)})

    def _admin_users_list(self):
        if not self._require_admin():
            return
        with _db() as conn:
            rows = conn.execute('''
                SELECT u.id,u.email,u.nickname,u.role,u.status,u.notes,u.created_at,
                       COALESCE(q.total_credits,0) total_credits,
                       COALESCE(q.used_credits,0) used_credits
                FROM users u LEFT JOIN quotas q ON q.user_id = u.id
                ORDER BY u.created_at DESC
            ''').fetchall()
        users = []
        for r in rows:
            users.append({
                'id': r['id'],
                'email': r['email'],
                'nickname': r['nickname'] or '',
                'role': r['role'],
                'status': r['status'],
                'notes': r['notes'] or '',
                'createdAt': r['created_at'],
                'totalCredits': int(r['total_credits']),
                'usedCredits': int(r['used_credits']),
                'remainingCredits': max(0, int(r['total_credits']) - int(r['used_credits'])),
            })
        self._json_response(200, {'ok': True, 'users': users})

    def _admin_usage_list(self):
        if not self._require_admin():
            return
        with _db() as conn:
            rows = conn.execute('''
                SELECT r.id,r.action,r.credits_used,r.task_title,r.created_at,u.email
                FROM usage_records r JOIN users u ON u.id = r.user_id
                ORDER BY r.created_at DESC LIMIT 100
            ''').fetchall()
        records = [dict(r) for r in rows]
        self._json_response(200, {'ok': True, 'records': records})

    def _app_settings_get(self):
        settings = _load_app_settings()
        self._json_response(200, {'ok': True, 'settings': settings})

    def _admin_settings_get(self):
        if not self._require_admin():
            return
        self._json_response(200, {'ok': True, 'settings': _load_app_settings()})

    def _admin_settings_update(self):
        if not self._require_admin():
            return
        try:
            data = self._read_json_body()
            settings = data.get('settings') if isinstance(data.get('settings'), dict) else data
            saved = _save_app_settings(settings or {})
            self._json_response(200, {'ok': True, 'settings': saved})
        except Exception as e:
            self._json_response(400, {'ok': False, 'error': str(e)})

    def _oss_sign_endpoint(self):
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        key = (query.get('key') or [''])[0].strip()
        if not key:
            self._json_response(400, {'ok': False, 'error': '缺少 oss key'})
            return
        url = self._oss_signed_url(key)
        if not url:
            self._json_response(400, {'ok': False, 'error': 'OSS 未启用或配置不完整'})
            return
        self._json_response(200, {'ok': True, 'url': url})

    def _admin_user_create(self):
        if not self._require_admin():
            return
        try:
            data = self._read_json_body()
            email = str(data.get('email') or '').strip()
            password = str(data.get('password') or '').strip()
            nickname = str(data.get('nickname') or '').strip()
            notes = str(data.get('notes') or '').strip()
            credits = max(0, int(data.get('credits') or 0))
            role = 'admin' if data.get('role') == 'admin' else 'user'
            if not email or not password:
                raise ValueError('账号和密码不能为空')
            with _db() as conn:
                cur = conn.execute(
                    'INSERT INTO users(email,password_hash,nickname,role,status,notes) VALUES(?,?,?,?,?,?)',
                    (email, _hash_password(password), nickname, role, 'active', notes)
                )
                conn.execute('INSERT INTO quotas(user_id,total_credits,used_credits) VALUES(?,?,0)', (cur.lastrowid, credits))
            self._json_response(200, {'ok': True})
        except sqlite3.IntegrityError:
            self._json_response(400, {'ok': False, 'error': '账号已存在'})
        except Exception as e:
            self._json_response(400, {'ok': False, 'error': str(e)})

    def _admin_quota_update(self):
        if not self._require_admin():
            return
        try:
            data = self._read_json_body()
            user_id = int(data.get('userId') or 0)
            total = max(0, int(data.get('totalCredits') or 0))
            with _db() as conn:
                conn.execute('INSERT OR IGNORE INTO quotas(user_id,total_credits,used_credits) VALUES(?,?,0)', (user_id, total))
                conn.execute('UPDATE quotas SET total_credits = ? WHERE user_id = ?', (total, user_id))
            self._json_response(200, {'ok': True})
        except Exception as e:
            self._json_response(400, {'ok': False, 'error': str(e)})

    def _admin_status_update(self):
        if not self._require_admin():
            return
        try:
            data = self._read_json_body()
            user_id = int(data.get('userId') or 0)
            status = 'disabled' if data.get('status') == 'disabled' else 'active'
            with _db() as conn:
                conn.execute('UPDATE users SET status = ? WHERE id = ?', (status, user_id))
            self._json_response(200, {'ok': True})
        except Exception as e:
            self._json_response(400, {'ok': False, 'error': str(e)})

    def _admin_password_update(self):
        if not self._require_admin():
            return
        try:
            data = self._read_json_body()
            user_id = int(data.get('userId') or 0)
            password = str(data.get('password') or '').strip()
            if len(password) < 6:
                raise ValueError('密码至少 6 位')
            with _db() as conn:
                conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (_hash_password(password), user_id))
            self._json_response(200, {'ok': True})
        except Exception as e:
            self._json_response(400, {'ok': False, 'error': str(e)})

    def _resolve_video_path(self, video_url):
        from urllib.parse import urlparse, unquote, parse_qs
        parsed = urlparse(video_url)
        if parsed.path.startswith('/cached-video/'):
            filename = os.path.basename(unquote(parsed.path))
            path = os.path.join(self._video_cache_dir(), filename)
            if not os.path.exists(path):
                raise FileNotFoundError('本地缓存视频不存在，请重新生成或先打开视频完成缓存')
            return path
        if parsed.scheme in ('http', 'https'):
            return self._cache_video(video_url, self._video_auth_header())
        raise ValueError('暂不支持的视频地址')

    def _resolve_image_path(self, image_url):
        from urllib.parse import urlparse, unquote
        parsed = urlparse(image_url)
        if parsed.scheme == 'data':
            header, payload = image_url.split(',', 1)
            ext = '.jpg' if 'jpeg' in header else '.webp' if 'webp' in header else '.png'
            path = os.path.join(self._video_cache_dir(), f'image_{hashlib.sha256(payload[:200].encode()).hexdigest()[:16]}{ext}')
            with open(path, 'wb') as f:
                f.write(base64.b64decode(payload))
            return path
        if parsed.path.startswith('/material-file/'):
            filename = os.path.basename(unquote(parsed.path))
            path = os.path.join(self._material_dir(), filename)
            if not os.path.exists(path):
                raise FileNotFoundError('本地图片素材不存在')
            return path
        if parsed.path.startswith('/cached-video/'):
            filename = os.path.basename(unquote(parsed.path))
            path = os.path.join(self._video_cache_dir(), filename)
            if not os.path.exists(path):
                raise FileNotFoundError('本地缓存图片不存在')
            return path
        if parsed.scheme in ('http', 'https'):
            cache_dir = self._video_cache_dir()
            key = hashlib.sha256(image_url.encode('utf-8')).hexdigest()[:16]
            ext = os.path.splitext(parsed.path)[1].lower()
            if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
                ext = '.jpg'
            path = os.path.join(cache_dir, f'image_{key}{ext}')
            if os.path.exists(path) and os.path.getsize(path) > 1024:
                return path
            ctx = ssl._create_unverified_context()
            req = urllib.request.Request(image_url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'image/*,*/*;q=0.8'})
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp, open(path, 'wb') as f:
                shutil.copyfileobj(resp, f)
            if not os.path.exists(path) or os.path.getsize(path) < 1024:
                raise RuntimeError('图片下载失败或文件过小')
            # P0-6.1: 自动入库素材库，避免AI生图额度浪费
            self._add_image_to_materials(path, image_url)
            return path
        raise ValueError('暂不支持的图片地址')

    def _kenburns_compose(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            image_urls = data.get('imageUrls') or []
            if not isinstance(image_urls, list):
                image_urls = []
            image_urls = [str(url).strip() for url in image_urls if str(url).strip()]
            if not image_urls:
                raise ValueError('缺少可用于 Ken Burns 成片的图片')
            try:
                target_duration = float(data.get('duration') or 10)
            except (TypeError, ValueError):
                target_duration = 10.0
            target_duration = max(3.0, min(120.0, target_duration))
            raw_durations = data.get('durations') or []
            raw_subtitles = data.get('subtitles') or []
            aspect = data.get('aspect') or '9:16'
            width, height = (1280, 720) if aspect == '16:9' else (1024, 1024) if aspect == '1:1' else (720, 1280)

            try:
                import imageio_ffmpeg
                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg_exe = shutil.which('ffmpeg')
            if not ffmpeg_exe:
                raise RuntimeError('未找到 ffmpeg，无法生成 Ken Burns 视频')

            entries = []
            for idx, url in enumerate(image_urls[:12]):
                try:
                    subtitle = ''
                    if isinstance(raw_subtitles, list) and idx < len(raw_subtitles):
                        subtitle = str(raw_subtitles[idx] or '').strip()
                    entries.append({
                        'path': self._resolve_image_path(url),
                        'subtitle': subtitle,
                    })
                except Exception:
                    continue
            paths = [entry['path'] for entry in entries]
            if not paths:
                raise RuntimeError('图片都无法下载或读取，无法生成 Ken Burns 视频')

            cache_dir = self._video_cache_dir()
            key = hashlib.sha256(('|'.join(paths) + str(time.time())).encode('utf-8')).hexdigest()[:16]
            output_mp4 = os.path.join(cache_dir, f'kenburns_{key}.mp4')
            durations = []
            if isinstance(raw_durations, list) and len(raw_durations) >= len(paths):
                for item in raw_durations[:len(paths)]:
                    try:
                        durations.append(max(0.8, float(item)))
                    except (TypeError, ValueError):
                        durations.append(0)
            if len(durations) != len(paths) or not any(v > 0 for v in durations):
                durations = [target_duration / len(paths)] * len(paths)
            else:
                total = sum(max(0, v) for v in durations) or target_duration
                durations = [max(0.8, max(0, v) / total * target_duration) for v in durations]
            fps = 15
            fontfile = ''
            for candidate in [
                '/System/Library/Fonts/PingFang.ttc',
                '/System/Library/Fonts/STHeiti Light.ttc',
                '/Library/Fonts/Arial Unicode.ttf',
            ]:
                if os.path.exists(candidate):
                    fontfile = candidate
                    break

            cmd = [ffmpeg_exe, '-y']
            for idx, path in enumerate(paths):
                cmd += ['-loop', '1', '-t', f'{durations[idx]:.3f}', '-i', path]

            filters = []
            labels = []
            work_width = width
            work_height = height
            if work_width % 2:
                work_width += 1
            if work_height % 2:
                work_height += 1
            for idx in range(len(paths)):
                label = f'kb{idx}'
                per_image = durations[idx]
                frame_count = max(45, int(per_image * fps))
                progress = f'on/{max(1, frame_count - 1)}'
                if idx % 3 == 0:
                    zoom_expr = f'1+0.24*{progress}'
                    x_expr = 'iw/2-(iw/zoom/2)'
                    y_expr = f'(ih-ih/zoom)*{progress}'
                elif idx % 3 == 1:
                    zoom_expr = f'1.24-0.18*{progress}'
                    x_expr = f'(iw-iw/zoom)*(1-{progress})'
                    y_expr = 'ih/2-(ih/zoom/2)'
                else:
                    zoom_expr = f'1.08+0.18*{progress}'
                    x_expr = f'(iw-iw/zoom)*{progress}'
                    y_expr = f'(ih-ih/zoom)*(1-{progress})'
                filters.append(
                    f'[{idx}:v]scale={work_width}:{work_height}:force_original_aspect_ratio=increase,'
                    f'crop={work_width}:{work_height},setsar=1,'
                    f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':"
                    f'd={frame_count}:s={width}x{height}:fps={fps},'
                    f'trim=duration={per_image:.3f},setpts=PTS-STARTPTS[{label}base]'
                )
                subtitle = (entries[idx].get('subtitle') or '').strip()
                if subtitle:
                    subtitle = self._compact_subtitle(subtitle)
                if subtitle:
                    escaped = self._ffmpeg_drawtext_escape(subtitle)
                    font_part = f":fontfile='{self._ffmpeg_drawtext_escape(fontfile)}'" if fontfile else ''
                    fontsize = max(28, int(height * 0.034))
                    boxborder = max(12, int(height * 0.014))
                    filters.append(
                        f'[{label}base]drawtext=text=\'{escaped}\'{font_part}:'
                        f'fontcolor=white:fontsize={fontsize}:'
                        f'box=1:boxcolor=black@0.48:boxborderw={boxborder}:'
                        f'x=(w-text_w)/2:y=h*0.78[{label}]'
                    )
                else:
                    filters.append(f'[{label}base]copy[{label}]')
                labels.append(f'[{label}]')
            filters.append(''.join(labels) + f'concat=n={len(paths)}:v=1:a=0[vout]')
            cmd += [
                '-filter_complex', ';'.join(filters),
                '-map', '[vout]',
                '-t', f'{target_duration:.2f}',
                '-r', str(fps),
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-crf', '24',
                '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
                '-an',
                output_mp4,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
            if res.returncode != 0 or not os.path.exists(output_mp4) or os.path.getsize(output_mp4) < 1024:
                raise RuntimeError((res.stderr or res.stdout or 'Ken Burns 成片失败').strip())

            self._json_response(200, {
                'ok': True,
                'url': f'/cached-video/{os.path.basename(output_mp4)}',
                'size': os.path.getsize(output_mp4),
                'duration': self._media_duration(ffmpeg_exe, output_mp4),
                'hasAudio': self._media_has_audio(ffmpeg_exe, output_mp4),
                'width': self._media_dimensions(ffmpeg_exe, output_mp4)[0],
                'height': self._media_dimensions(ffmpeg_exe, output_mp4)[1],
                'imageCount': len(paths),
            })
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _tts_generate(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            text = (data.get('text') or '').strip()
            voice = self._normalize_tts_voice((data.get('voice') or 'longanyang').strip())
            tts_auth = self.headers.get('X-TTS-Auth', '').strip()
            if not text:
                raise ValueError('缺少配音文本')

            cache_dir = self._video_cache_dir()
            key = hashlib.sha256((text + voice + str(time.time())).encode('utf-8')).hexdigest()[:16]
            audio_file = os.path.join(cache_dir, f'tts_{key}.mp3')
            if not tts_auth:
                raise RuntimeError('缺少百炼 Key，无法生成配音预听。系统已移除本机语音兜底，避免客户环境依赖 macOS。')
            provider = self._create_dashscope_tts(text, voice, tts_auth, audio_file)
            warning = ''

            try:
                import imageio_ffmpeg
                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg_exe = shutil.which('ffmpeg')
            duration = self._media_duration(ffmpeg_exe, audio_file) if ffmpeg_exe else None
            self._json_response(200, {
                'ok': True,
                'audioUrl': f'/cached-video/{os.path.basename(audio_file)}',
                'durationSeconds': duration,
                'totalCharCount': len(text),
                'size': os.path.getsize(audio_file),
                'provider': provider,
                'warning': warning,
            })
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _voice_compose(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            text = (data.get('text') or '').strip()
            video_url = (data.get('videoUrl') or '').strip()
            voice = self._normalize_tts_voice((data.get('voice') or 'longanyang').strip())
            audio_led = bool(data.get('audioLed'))
            raw_subtitles = data.get('subtitles') or []
            raw_subtitle_durations = data.get('subtitleDurations') or []
            requested_duration = data.get('targetDuration')
            try:
                requested_duration = float(requested_duration) if requested_duration else None
            except (TypeError, ValueError):
                requested_duration = None
            tts_auth = self.headers.get('X-TTS-Auth', '').strip()
            if not text:
                raise ValueError('缺少配音文本')
            if not video_url:
                raise ValueError('缺少视频地址')

            cache_dir = self._video_cache_dir()
            source_video = self._resolve_video_path(video_url)
            key = hashlib.sha256((source_video + text + str(time.time())).encode('utf-8')).hexdigest()[:16]
            audio_file = os.path.join(cache_dir, f'voice_{key}.mp3')
            output_mp4 = os.path.join(cache_dir, f'voiced_{key}.mp4')
            tts_warning = ''

            if not tts_auth:
                raise RuntimeError('缺少百炼 Key，无法生成配音。系统已移除本机语音兜底，避免客户环境依赖 macOS。')
            self._create_dashscope_tts(text, voice, tts_auth, audio_file)

            try:
                import imageio_ffmpeg
                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg_exe = shutil.which('ffmpeg')
            if not ffmpeg_exe:
                raise RuntimeError('未找到 ffmpeg，无法把配音合成进视频')

            video_duration = self._media_duration(ffmpeg_exe, source_video) or 0
            audio_duration = self._media_duration(ffmpeg_exe, audio_file) or 0
            max_extension = 2.0
            if audio_led and audio_duration > 0:
                natural_duration = max(video_duration, audio_duration + 0.8)
                if requested_duration:
                    natural_duration = min(natural_duration, max(video_duration, requested_duration + 0.8))
                target_duration = min(natural_duration, video_duration + max_extension)
            else:
                natural_duration = max(video_duration, audio_duration) + 0.8
                if requested_duration:
                    natural_duration = min(natural_duration, requested_duration + 0.8)
                target_duration = min(natural_duration, video_duration + max_extension)
            target_duration = max(1.5, target_duration)
            pad_duration = max(0, target_duration - video_duration)
            audio_fade_start = max(0, target_duration - 0.8)
            video_filter = f'tpad=stop_mode=clone:stop_duration={pad_duration:.2f}'
            audio_filter = f'apad=pad_dur=0.8,afade=t=out:st={audio_fade_start:.2f}:d=0.8'
            subtitle_filters = []
            video_out_label = 'vbase'
            if isinstance(raw_subtitles, list):
                subtitle_texts = [str(item or '').strip() for item in raw_subtitles]
            else:
                subtitle_texts = []
            if not any(subtitle_texts) and text:
                subtitle_texts = self._subtitle_chunks_from_text(text)
                raw_subtitle_durations = []
                print(f'[voice-compose] subtitle fallback from voice text, count={len(subtitle_texts)}')
            subtitle_durations = []
            if isinstance(raw_subtitle_durations, list):
                for item in raw_subtitle_durations[:len(subtitle_texts)]:
                    try:
                        subtitle_durations.append(max(0.5, float(item)))
                    except (TypeError, ValueError):
                        subtitle_durations.append(0)
            if subtitle_texts:
                if len(subtitle_durations) != len(subtitle_texts) or not any(v > 0 for v in subtitle_durations):
                    subtitle_durations = [target_duration / max(1, len(subtitle_texts))] * len(subtitle_texts)
                else:
                    total_subtitle_duration = sum(max(0, v) for v in subtitle_durations) or target_duration
                    subtitle_durations = [max(0.5, max(0, v) / total_subtitle_duration * target_duration) for v in subtitle_durations]
                fontfile = self._find_chinese_font()
                font_part = f":fontfile='{self._ffmpeg_drawtext_escape(fontfile)}'" if fontfile else ''
                dimensions = self._media_dimensions(ffmpeg_exe, source_video)
                video_height = dimensions[1] or 1280
                fontsize = max(28, int(video_height * 0.034))
                boxborder = max(10, int(video_height * 0.012))
                offset = 0.0
                current_label = 'vbase'
                subtitle_index = 0
                for idx, subtitle in enumerate(subtitle_texts):
                    duration = subtitle_durations[idx] if idx < len(subtitle_durations) else target_duration / max(1, len(subtitle_texts))
                    start = max(0.0, offset)
                    end = min(target_duration, offset + duration)
                    offset += duration
                    lines = self._caption_lines(subtitle, max_chars=30, line_chars=15)
                    if not lines or end <= start:
                        continue
                    for row, line in enumerate(lines):
                        safe_line = self._ffmpeg_drawtext_escape(line)
                        next_label = f'vsub{subtitle_index}'
                        y_expr = f'h*0.76+{row}*{int(fontsize * 1.28)}'
                        subtitle_filters.append(
                            f'[{current_label}]drawtext=text=\'{safe_line}\'{font_part}:'
                            f'fontcolor=white:fontsize={fontsize}:'
                            f'box=1:boxcolor=black@0.48:boxborderw={boxborder}:'
                            f'x=(w-text_w)/2:y={y_expr}:'
                            f"enable='between(t,{start:.2f},{end:.2f})'[{next_label}]"
                        )
                        current_label = next_label
                        subtitle_index += 1
                video_out_label = current_label
            filter_complex = f'[0:v]{video_filter}[vbase];'
            if subtitle_filters:
                filter_complex += ';'.join(subtitle_filters) + ';'
            filter_complex += f'[1:a]{audio_filter}[a]'
            compose_cmd = [
                ffmpeg_exe, '-y',
                '-i', source_video,
                '-i', audio_file,
                '-filter_complex', filter_complex,
                '-map', f'[{video_out_label}]',
                '-map', '[a]',
                '-t', f'{target_duration:.2f}',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', '20',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-movflags', '+faststart',
                output_mp4,
            ]
            compose_res = subprocess.run(compose_cmd, capture_output=True, text=True, timeout=180)
            if compose_res.returncode != 0:
                raise RuntimeError((compose_res.stderr or compose_res.stdout or '视频音频合成失败').strip())
            if not os.path.exists(output_mp4) or os.path.getsize(output_mp4) < 1024:
                raise RuntimeError('合成后的视频文件异常')

            self._json_response(200, {
                'ok': True,
                'url': f'/cached-video/{os.path.basename(output_mp4)}',
                'size': os.path.getsize(output_mp4),
                'duration': self._media_duration(ffmpeg_exe, output_mp4),
                'hasAudio': self._media_has_audio(ffmpeg_exe, output_mp4),
                'width': self._media_dimensions(ffmpeg_exe, output_mp4)[0],
                'height': self._media_dimensions(ffmpeg_exe, output_mp4)[1],
                'subtitleCount': len([s for s in subtitle_texts if str(s).strip()]),
                'warning': tts_warning,
            })
        except Exception as e:
            _server_logger.exception('voice-compose failed: %s', e)
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _video_stitch(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            video_urls = data.get('videoUrls') or []
            if not isinstance(video_urls, list) or len(video_urls) < 2:
                raise ValueError('至少需要2段视频才能拼接')

            try:
                import imageio_ffmpeg
                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg_exe = shutil.which('ffmpeg')
            if not ffmpeg_exe:
                raise RuntimeError('未找到 ffmpeg，无法拼接视频')

            cache_dir = self._video_cache_dir()
            paths = [self._resolve_video_path(str(url)) for url in video_urls]
            key = hashlib.sha256(('|'.join(paths) + str(time.time())).encode('utf-8')).hexdigest()[:16]
            list_file = os.path.join(cache_dir, f'stitch_{key}.txt')
            output_mp4 = os.path.join(cache_dir, f'stitched_{key}.mp4')
            with open(list_file, 'w', encoding='utf-8') as f:
                for path in paths:
                    safe_path = path.replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")

            concat_cmd = [
                ffmpeg_exe, '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', list_file,
                '-vf', 'scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1',
                '-r', '30',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', '21',
                '-an',
                '-movflags', '+faststart',
                output_mp4,
            ]
            concat_res = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=300)
            if concat_res.returncode != 0 or not os.path.exists(output_mp4) or os.path.getsize(output_mp4) < 1024:
                raise RuntimeError((concat_res.stderr or concat_res.stdout or '视频拼接失败').strip())

            self._json_response(200, {
                'ok': True,
                'url': f'/cached-video/{os.path.basename(output_mp4)}',
                'size': os.path.getsize(output_mp4),
                'duration': self._media_duration(ffmpeg_exe, output_mp4),
                'hasAudio': self._media_has_audio(ffmpeg_exe, output_mp4),
                'width': self._media_dimensions(ffmpeg_exe, output_mp4)[0],
                'height': self._media_dimensions(ffmpeg_exe, output_mp4)[1],
            })
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _mix_compose(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            clips = data.get('clips') or []
            if not isinstance(clips, list) or not clips:
                raise ValueError('缺少混剪素材')
            try:
                target_duration = float(data.get('duration') or 15)
            except (TypeError, ValueError):
                target_duration = 15.0
            target_duration = max(3.0, min(120.0, target_duration))
            aspect = data.get('aspect') or '9:16'
            width, height = (1280, 720) if aspect == '16:9' else (1080, 1080) if aspect == '1:1' else (720, 1280)
            brand = (data.get('brandName') or '').strip()

            try:
                import imageio_ffmpeg
                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg_exe = shutil.which('ffmpeg')
            if not ffmpeg_exe:
                raise RuntimeError('未找到 ffmpeg，无法合成混剪视频')

            items = self._load_material_index()
            by_stored = {i.get('storedName'): i for i in items if i.get('storedName')}
            selected = []
            for idx, clip in enumerate(clips[:20]):
                stored = os.path.basename(str(clip.get('storedName') or ''))
                item = by_stored.get(stored)
                if not item:
                    continue
                path = os.path.join(self._material_dir(), stored)
                if not os.path.exists(path) or os.path.getsize(path) < 1024:
                    continue
                kind = item.get('kind') or ('video' if str(item.get('type') or '').startswith('video/') else 'image')
                duration = clip.get('duration')
                try:
                    duration = float(duration)
                except (TypeError, ValueError):
                    duration = None
                selected.append({
                    'path': path,
                    'kind': kind,
                    'duration': max(1.0, min(12.0, duration or 3.0)),
                    'caption': str(clip.get('caption') or clip.get('voiceover') or '')[:120],
                    'item': item,
                })
            if not selected:
                raise ValueError('没有找到可用的本地素材')

            duration_total = sum(c['duration'] for c in selected) or target_duration
            for c in selected:
                c['duration'] = max(0.8, c['duration'] / duration_total * target_duration)

            cache_dir = self._video_cache_dir()
            key = hashlib.sha256((json.dumps([c['item'].get('storedName') for c in selected], ensure_ascii=False) + str(time.time())).encode('utf-8')).hexdigest()[:16]
            output_mp4 = os.path.join(cache_dir, f'local_mix_{key}.mp4')

            cmd = [ffmpeg_exe, '-y']
            for clip in selected:
                dur = f"{clip['duration']:.3f}"
                if clip['kind'] == 'image':
                    cmd += ['-loop', '1', '-t', dur, '-i', clip['path']]
                else:
                    cmd += ['-stream_loop', '-1', '-t', dur, '-i', clip['path']]

            filters = []
            labels = []
            scale_crop = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1"
            )
            for idx, clip in enumerate(selected):
                dur = clip['duration']
                label = f'v{idx}'
                filters.append(
                    f'[{idx}:v]{scale_crop},trim=duration={dur:.3f},'
                    f'setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.15,'
                    f'fade=t=out:st={max(0.1, dur - 0.2):.3f}:d=0.2[{label}]'
                )
                labels.append(f'[{label}]')
            filters.append(''.join(labels) + f'concat=n={len(selected)}:v=1:a=0[vbase]')
            out_label = 'vbase'

            if brand:
                safe_brand = self._ffmpeg_drawtext_escape(brand[:24])
                font_path = self._find_chinese_font()
                font_arg = f":fontfile='{self._ffmpeg_drawtext_escape(font_path)}'" if font_path else ''
                filters.append(
                    f"[vbase]drawtext=text='{safe_brand}'{font_arg}:x=w-tw-28:y=28:"
                    f"fontcolor=white:fontsize=26:box=1:boxcolor=black@0.35:boxborderw=12[vout]"
                )
                out_label = 'vout'

            captions = []
            offset = 0.0
            for clip in selected:
                caption_lines = self._caption_lines(clip.get('caption') or '', max_chars=28, line_chars=14)
                if caption_lines:
                    captions.append((offset, offset + clip['duration'], caption_lines))
                offset += clip['duration']
            if captions:
                font_path = self._find_chinese_font()
                font_arg = f":fontfile='{self._ffmpeg_drawtext_escape(font_path)}'" if font_path else ''
                caption_label = out_label
                line_idx = 0
                for start, end, lines in captions:
                    fontsize = max(24, min(34, int(height * 0.03)))
                    boxborder = max(10, int(height * 0.012))
                    base_y = f"h-{int(height * 0.16)}"
                    if len(lines) == 2:
                        base_y = f"h-{int(height * 0.19)}"
                    for row, caption in enumerate(lines):
                        next_label = f'vcap{line_idx}'
                        safe_caption = self._ffmpeg_drawtext_escape(caption)
                        y_expr = f"{base_y}+{row * int(fontsize * 1.45)}"
                        filters.append(
                            f"[{caption_label}]drawtext=text='{safe_caption}'{font_arg}:"
                            f"enable='between(t,{start:.3f},{end:.3f})':"
                            f"fontcolor=white:fontsize={fontsize}:box=1:boxcolor=black@0.50:"
                            f"boxborderw={boxborder}:x=max(24\\,(w-text_w)/2):y={y_expr}[{next_label}]"
                        )
                        caption_label = next_label
                        line_idx += 1
                out_label = caption_label

            cmd += [
                '-filter_complex', ';'.join(filters),
                '-map', f'[{out_label}]',
                '-t', f'{target_duration:.2f}',
                '-r', '30',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', '21',
                '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
                '-an',
                output_mp4,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
            if res.returncode != 0 or not os.path.exists(output_mp4) or os.path.getsize(output_mp4) < 1024:
                raise RuntimeError((res.stderr or res.stdout or '本地素材混剪失败').strip()[-1000:])

            self._json_response(200, {
                'ok': True,
                'url': f'/cached-video/{os.path.basename(output_mp4)}',
                'size': os.path.getsize(output_mp4),
                'duration': self._media_duration(ffmpeg_exe, output_mp4),
                'hasAudio': self._media_has_audio(ffmpeg_exe, output_mp4),
                'width': self._media_dimensions(ffmpeg_exe, output_mp4)[0],
                'height': self._media_dimensions(ffmpeg_exe, output_mp4)[1],
                'clips': [c['item'].get('storedName') for c in selected],
                'localClipCount': len(selected),
            })
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _pexels_compose(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
            api_key = (data.get('apiKey') or '').strip()
            if not api_key:
                raise ValueError('缺少 Pexels API Key')
            queries = data.get('queries') or []
            if isinstance(queries, str):
                queries = [queries]
            queries = [str(q).strip() for q in queries if str(q).strip()]
            if not queries:
                raise ValueError('缺少素材检索关键词')
            try:
                target_duration = float(data.get('duration') or 10)
            except (TypeError, ValueError):
                target_duration = 10.0
            target_duration = max(3.0, min(120.0, target_duration))
            aspect = data.get('aspect') or '9:16'
            width, height = (1280, 720) if aspect == '16:9' else (720, 1280)
            brand = (data.get('brandName') or '').strip()
            material_plan = data.get('materialPlan') or {}

            try:
                import imageio_ffmpeg
                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg_exe = shutil.which('ffmpeg')
            if not ffmpeg_exe:
                raise RuntimeError('未找到 ffmpeg，无法合成 Pexels 素材视频')

            cache_dir = self._video_cache_dir()
            target_count = max(int(material_plan.get('minClipCount') or 3), min(6, len(queries) + 2))
            local_candidates = self._local_material_video_paths(target_count, queries, material_plan)
            pexels_candidates = self._pexels_find_and_download_clips(api_key, queries, cache_dir, target_count=target_count)
            local_clip_count = len(local_candidates)
            pexels_clip_count = len(pexels_candidates)
            clips = self._merge_material_candidates(local_candidates, pexels_candidates, target_count)
            if not clips:
                raise RuntimeError('Pexels 未找到可用视频素材，请换更具体的关键词')
            material_issues = []
            if len(clips) < target_count:
                material_issues.append(f'可用素材只有 {len(clips)} 段，低于最低要求 {target_count} 段')
            if material_plan.get('requiresChineseBusinessContext'):
                if local_clip_count == 0:
                    material_issues.append(f'企业/中文语境内容没有命中已识别的本地企业素材；已搜索到 Pexels {pexels_clip_count} 段，但不适合作为主素材')
                elif pexels_clip_count > local_clip_count:
                    material_issues.append(f'企业/中文语境内容不允许 Pexels 成为主素材：本地合格候选 {local_clip_count} 段，Pexels 候选 {pexels_clip_count} 段；将改用百炼/千问生态 AI 视频生成')
                elif local_clip_count + pexels_clip_count < target_count:
                    material_issues.append(f'本地 + Pexels 候选素材只有 {local_clip_count + pexels_clip_count} 段，低于最低要求 {target_count} 段')
            if material_issues:
                self._json_response(422, {
                    'ok': False,
                    'fallback': 'ai_video',
                    'error': '；'.join(material_issues),
                    'materialQa': {
                        'targetClipCount': target_count,
                        'foundClipCount': len(clips),
                        'localClipCount': local_clip_count,
                        'pexelsClipCount': pexels_clip_count,
                        'pexelsSearched': True,
                        'queries': queries,
                    },
                })
                return

            key = hashlib.sha256((json.dumps(queries, ensure_ascii=False) + str(time.time())).encode('utf-8')).hexdigest()[:16]
            output_mp4 = os.path.join(cache_dir, f'pexels_mix_{key}.mp4')
            clip_count = max(1, min(len(clips), int(target_duration // 2) or 1))
            selected = clips[:clip_count]
            per_clip = target_duration / len(selected)

            cmd = [ffmpeg_exe, '-y']
            for path in selected:
                cmd += ['-stream_loop', '-1', '-i', path]

            filters = []
            labels = []
            scale_crop = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1"
            )
            for idx in range(len(selected)):
                label = f'v{idx}'
                filters.append(
                    f'[{idx}:v]{scale_crop},trim=duration={per_clip:.3f},'
                    f'setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.2,'
                    f'fade=t=out:st={max(0.1, per_clip - 0.25):.3f}:d=0.25[{label}]'
                )
                labels.append(f'[{label}]')
            filters.append(''.join(labels) + f'concat=n={len(selected)}:v=1:a=0[vbase]')
            out_label = 'vbase'

            cmd += [
                '-filter_complex', ';'.join(filters),
                '-map', f'[{out_label}]',
                '-t', f'{target_duration:.2f}',
                '-r', '30',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', '21',
                '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
                '-an',
                output_mp4,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
            if res.returncode != 0 or not os.path.exists(output_mp4) or os.path.getsize(output_mp4) < 1024:
                raise RuntimeError((res.stderr or res.stdout or 'Pexels 素材混剪失败').strip())

            self._json_response(200, {
                'ok': True,
                'url': f'/cached-video/{os.path.basename(output_mp4)}',
                'size': os.path.getsize(output_mp4),
                'duration': self._media_duration(ffmpeg_exe, output_mp4),
                'hasAudio': self._media_has_audio(ffmpeg_exe, output_mp4),
                'width': self._media_dimensions(ffmpeg_exe, output_mp4)[0],
                'height': self._media_dimensions(ffmpeg_exe, output_mp4)[1],
                'clips': [os.path.basename(p) for p in selected],
                'localClipCount': len([p for p in selected if os.path.abspath(p).startswith(os.path.abspath(self._material_dir()))]),
                'pexelsClipCount': len([p for p in selected if os.path.basename(p).startswith('pexels_')]),
                'queries': queries,
                'materialQa': {
                    'targetClipCount': target_count,
                    'foundClipCount': len(clips),
                    'localClipCount': local_clip_count,
                    'pexelsClipCount': pexels_clip_count,
                    'pexelsSearched': True,
                    'fallback': False,
                    'issues': [],
                },
            })
        except Exception as e:
            self._json_response(500, {'ok': False, 'error': str(e)})

    def _merge_material_candidates(self, local_candidates, pexels_candidates, limit):
        selected = []
        seen = set()

        def add(path):
            key = os.path.abspath(path)
            if key in seen:
                return
            if os.path.exists(path) and os.path.getsize(path) > 1024:
                seen.add(key)
                selected.append(path)

        # Prefer a verified local opening when available, then force Pexels into
        # the candidate pool so local素材不会因为“数量够”绕过公共素材检索。
        if local_candidates:
            add(local_candidates[0])
        for path in pexels_candidates:
            if len(selected) >= limit:
                break
            add(path)
        for path in local_candidates[1:]:
            if len(selected) >= limit:
                break
            add(path)
        return selected[:limit]

    def _local_material_video_paths(self, limit, queries=None, material_plan=None):
        material_plan = material_plan or {}
        query_text = ' '.join(queries or [])
        query_tokens = self._tokenize_material_text(query_text)
        required_domain = self._infer_required_material_domain(query_text)
        forbidden_tokens = self._tokenize_material_text(' '.join(material_plan.get('forbidden') or []))
        must_tokens = self._tokenize_material_text(' '.join(material_plan.get('mustHave') or []))
        requires_verified = bool(material_plan.get('requiresChineseBusinessContext'))
        items = self._load_material_index()
        scored = []
        for item in items:
            if item.get('kind') != 'video':
                continue
            stored = os.path.basename(str(item.get('storedName') or ''))
            if not stored:
                continue
            path = os.path.join(self._material_dir(), stored)
            if os.path.exists(path) and os.path.getsize(path) > 1024:
                analysis = item.get('analysis') if isinstance(item.get('analysis'), dict) else {}
                if requires_verified and not analysis:
                    continue
                text = ' '.join([
                    item.get('filename') or '',
                    ' '.join(item.get('tags') or []),
                    analysis.get('sceneDesc') or '',
                    analysis.get('bestUse') or '',
                    ' '.join(analysis.get('autoTags') or []),
                ])
                material_tokens = self._tokenize_material_text(text)
                if required_domain and not material_tokens.intersection(required_domain):
                    continue
                if forbidden_tokens and material_tokens.intersection(forbidden_tokens):
                    continue
                if requires_verified:
                    quality = str(analysis.get('quality') or '').lower()
                    risks = ' '.join(analysis.get('risks') or [])
                    risk_tokens = self._tokenize_material_text(risks)
                    if quality in ('low', 'bad', 'poor', '差', '低'):
                        continue
                    if forbidden_tokens and risk_tokens.intersection(forbidden_tokens):
                        continue
                hits = query_tokens.intersection(material_tokens)
                must_hits = must_tokens.intersection(material_tokens) if must_tokens else set()
                filtered_hits = {t for t in hits if t not in self._generic_material_tokens()}
                domain_hits = required_domain.intersection(material_tokens) if required_domain else set()
                if (filtered_hits or domain_hits) and (not must_tokens or must_hits):
                    if not required_domain and len(filtered_hits) < 2:
                        continue
                    score = len(filtered_hits) * 10 + len(domain_hits) * 18 + len(must_hits) * 8 + min(5, len(item.get('analysis', {}).get('autoTags') or []))
                    scored.append((score, item.get('createdAt') or 0, path))
        scored.sort(reverse=True)
        return [path for _, _, path in scored[:limit]]

    def _tokenize_material_text(self, text):
        raw = str(text or '').lower()
        tokens = set(re.findall(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]{2,}', raw))
        aliases = {
            'office': ['办公室', '办公', '会议室'],
            'business': ['企业', '商务', '公司'],
            'team': ['团队', '员工', '职场'],
            'meeting': ['会议', '讨论', '沟通'],
            'technology': ['科技', '技术', 'ai', '人工智能'],
            'computer': ['电脑', '屏幕', '办公'],
            'customer': ['客户', '客服', '服务'],
            'product': ['产品', '展示', '演示'],
            'data': ['数据', '分析', '报表'],
            'tissue': ['纸巾', '抽纸', '卫生纸', '卷纸', '湿巾', '洗脸巾', '面巾纸', '厨房纸', '柔纸巾', '日化', '快消', '家清'],
            'household': ['家庭', '家居', '清洁', '擦拭', '客厅', '厨房', '卫生间', '餐桌'],
            'packaging': ['包装', '外包装', '产品包装', '陈列', '货架', '堆头'],
            'fresh': ['生鲜', '水果', '蔬菜', '鲜肉', '水产', '海鲜', '冷鲜', '农产品', '菜市场', '果蔬', '超市', '货架', '称重', '收银'],
            'food': ['餐饮', '餐厅', '厨房', '菜品', '食品', '食材', '外卖'],
            'retail': ['零售', '门店', '店铺', '货架', '陈列', '收银', '会员', '到店', '超市'],
            'factory': ['工厂', '车间', '生产线', '设备', '制造', '质检', '仓库'],
            'beauty': ['美业', '美容', '美甲', '美发', '护肤', '医美'],
            'education': ['教育', '学校', '课堂', '老师', '学生', '培训', '课程'],
            'medical': ['医疗', '医院', '门诊', '医生', '护士', '诊所', '药店'],
            'realestate': ['房产', '楼盘', '小区', '户型', '装修', '家装'],
            'auto': ['汽车', '试驾', '4s店', '维修', '保养', '新能源车'],
        }
        expanded = set(tokens)
        for key, values in aliases.items():
            if key in tokens or any(v in raw for v in values):
                expanded.add(key)
                expanded.update(values)
        return expanded

    def _generic_material_tokens(self):
        return {
            '中国', '现代', '画面', '镜头', '场景', '空间', '人物', '专业', '真实', '自然',
            '企业', '商务', '公司', '员工', '团队', '办公室', '办公', '职场', '客户',
            '品牌', '产品', '展示', '服务', '视频', '图片', '竖版', '横版',
        }

    def _infer_required_material_domain(self, text):
        raw = str(text or '').lower()
        domains = {
            'tissue': ['纸巾', '抽纸', '卫生纸', '卷纸', '湿巾', '洗脸巾', '面巾纸', '厨房纸', '柔纸巾', '日化', '快消', '家清', '包装', '货架', '家庭', '清洁'],
            'fresh': ['生鲜', '水果', '蔬菜', '鲜肉', '水产', '海鲜', '冷鲜', '农产品', '菜市场', '果蔬', '门店', '超市', '货架', '称重', '收银'],
            'food': ['餐饮', '饭店', '餐厅', '厨房', '菜品', '外卖', '食品', '食材', '烘焙', '奶茶', '咖啡'],
            'retail': ['零售', '门店', '店铺', '导购', '货架', '陈列', '收银', '会员', '到店'],
            'factory': ['工厂', '车间', '生产线', '设备', '制造', '质检', '仓库', '发货'],
            'beauty': ['美业', '美容', '美甲', '美发', '护肤', '医美'],
            'education': ['教育', '学校', '课堂', '老师', '学生', '培训', '课程'],
            'medical': ['医疗', '医院', '门诊', '医生', '护士', '诊所', '药店'],
            'realestate': ['房产', '楼盘', '小区', '户型', '装修', '家装', '看房'],
            'auto': ['汽车', '试驾', '4s店', '维修', '保养', '新能源车'],
        }
        required = set()
        for key, words in domains.items():
            if any(w in raw for w in words):
                required.add(key)
                required.update(words)
        return required

    def _pexels_find_and_download_clips(self, api_key, queries, cache_dir, target_count=4):
        from urllib.parse import urlencode
        ctx = ssl._create_unverified_context()
        found = []
        seen = set()
        search_queries = queries[:]
        joined = ' '.join(search_queries).lower()
        non_office_domain = any(x in joined for x in ('tissue', 'paper', 'household', 'grocery', 'restaurant', 'food', 'product packaging', 'store shelf'))
        if not non_office_domain and not any(q.lower() in ('office', 'business', 'technology', 'computer') for q in search_queries):
            search_queries += ['business office', 'technology office', 'computer work']
        for query in search_queries:
            if len(found) >= target_count:
                break
            params = urlencode({
                'query': query,
                'orientation': 'portrait',
                'per_page': 8,
            })
            req = urllib.request.Request(
                f'https://api.pexels.com/videos/search?{params}',
                headers={
                    'Authorization': api_key,
                    'User-Agent': 'AgentWorkflow/1.0',
                    'Accept': 'application/json',
                },
            )
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
                    payload = json.loads(resp.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                detail = e.read().decode('utf-8', errors='replace')
                raise RuntimeError(f'Pexels 搜索失败 HTTP {e.code}: {detail[:300]}')
            except Exception:
                continue
            for video in payload.get('videos', []):
                if len(found) >= target_count:
                    break
                video_id = str(video.get('id') or '')
                if video_id in seen:
                    continue
                candidate = self._choose_pexels_video_file(video.get('video_files') or [])
                if not candidate:
                    continue
                try:
                    path = self._download_pexels_clip(candidate, cache_dir, video_id)
                    if path:
                        seen.add(video_id)
                        found.append(path)
                except Exception:
                    continue
        return found

    def _choose_pexels_video_file(self, files):
        candidates = []
        for item in files:
            link = item.get('link') or ''
            file_type = item.get('file_type') or ''
            if not link or ('mp4' not in file_type and '.mp4' not in link.lower()):
                continue
            width = int(item.get('width') or 0)
            height = int(item.get('height') or 0)
            if width < 360 or height < 360:
                continue
            score = 0
            if height >= width:
                score += 1000
            score += min(width * height, 1920 * 1080) // 1000
            candidates.append((score, link))
        candidates.sort(reverse=True)
        return candidates[0][1] if candidates else None

    def _download_pexels_clip(self, url, cache_dir, video_id):
        key = hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]
        path = os.path.join(cache_dir, f'pexels_{video_id or key}_{key}.mp4')
        if os.path.exists(path) and os.path.getsize(path) > 1024:
            return path
        tmp = path + '.part'
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'video/mp4,video/*;q=0.9,*/*;q=0.8',
        })
        last_error = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=180) as resp, open(tmp, 'wb') as f:
                    shutil.copyfileobj(resp, f, length=1024 * 1024)
                last_error = None
                break
            except Exception as e:
                last_error = e
                time.sleep(1)
        if last_error:
            raise last_error
        if os.path.getsize(tmp) < 1024:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None
        os.replace(tmp, path)
        return path

    def _media_duration(self, ffmpeg_exe, path):
        if not ffmpeg_exe:
            return None
        probe = subprocess.run([ffmpeg_exe, '-i', path], capture_output=True, text=True, timeout=30)
        text = (probe.stderr or '') + (probe.stdout or '')
        match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', text)
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def _media_has_audio(self, ffmpeg_exe, path):
        if not ffmpeg_exe:
            return None
        probe = subprocess.run([ffmpeg_exe, '-i', path], capture_output=True, text=True, timeout=30)
        text = (probe.stderr or '') + (probe.stdout or '')
        return bool(re.search(r'Stream #\d+:\d+.*Audio:', text))

    def _media_dimensions(self, ffmpeg_exe, path):
        if not ffmpeg_exe:
            return (None, None)
        probe = subprocess.run([ffmpeg_exe, '-i', path], capture_output=True, text=True, timeout=30)
        text = (probe.stderr or '') + (probe.stdout or '')
        match = re.search(r'Video:.*?,\s*(\d{2,5})x(\d{2,5})\s', text)
        if not match:
            return (None, None)
        return (int(match.group(1)), int(match.group(2)))

    def _ffmpeg_drawtext_escape(self, text):
        return text.replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'").replace('%', '\\%')

    def _find_chinese_font(self):
        for candidate in [
            '/System/Library/Fonts/PingFang.ttc',
            '/System/Library/Fonts/STHeiti Light.ttc',
            '/Library/Fonts/Arial Unicode.ttf',
            '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        ]:
            if os.path.exists(candidate):
                return candidate
        return ''

    def _compact_subtitle(self, text, max_chars=28):
        clean = re.sub(r'\s+', '', str(text or '')).strip()
        if not clean:
            return ''
        if re.search(r'(无字幕|没有字幕|不加字幕|无需字幕|保留.*尾音|尾音|可为空|空字符串|字幕短句|无需显示|不显示文字|不显示字幕)', clean, re.I):
            return ''
        if re.match(r'^[（(].*[）)]$', clean) and len(clean) > 8:
            return ''
        if len(clean) <= max_chars:
            return clean
        return clean[:max_chars - 1] + '…'

    def _normalize_tts_voice(self, voice):
        value = str(voice or '').strip()
        if not value or value == 'longwan':
            return 'longanyang'
        if value not in ('longanyang',):
            return 'longanyang'
        return value

    def _caption_lines(self, text, max_chars=28, line_chars=14):
        clean = self._compact_subtitle(text, max_chars=max_chars)
        if not clean:
            return []
        if len(clean) <= line_chars:
            return [clean]
        return [clean[:line_chars], clean[line_chars:line_chars * 2]]

    def _subtitle_chunks_from_text(self, text, chunk_chars=14, max_chunks=6):
        clean = re.sub(r'[#@]\S+', '', str(text or ''))
        clean = re.sub(r'[，,。！？!?；;：:、\s]+', '', clean).strip()
        if not clean:
            return []
        chunks = [clean[i:i + chunk_chars] for i in range(0, min(len(clean), chunk_chars * max_chunks), chunk_chars)]
        return [chunk for chunk in chunks if self._compact_subtitle(chunk)]

    def _create_dashscope_tts(self, text, voice, auth_header, output_path):
        voice = self._normalize_tts_voice(voice)
        ctx = ssl._create_unverified_context()
        errors = []
        for model in ('cosyvoice-v3-flash', 'cosyvoice-v2', 'cosyvoice-v1'):
            payload = {
                'model': model,
                'input': {
                    'text': text,
                    'voice': voice,
                    'format': 'mp3',
                    'sample_rate': 24000,
                    'volume': 70,
                    'rate': 1.08,
                    'language_hints': ['zh'],
                },
            }
            req = urllib.request.Request(
                'https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer',
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers={
                    'Authorization': auth_header,
                    'Content-Type': 'application/json',
                },
                method='POST',
            )
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                audio_url = data.get('output', {}).get('audio', {}).get('url')
                if not audio_url:
                    raise RuntimeError('未返回音频URL: ' + json.dumps(data, ensure_ascii=False)[:240])
                audio_req = urllib.request.Request(audio_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(audio_req, context=ctx, timeout=120) as audio_resp, open(output_path, 'wb') as f:
                    shutil.copyfileobj(audio_resp, f, length=1024 * 512)
                return model
            except urllib.error.HTTPError as e:
                detail = e.read().decode('utf-8', errors='replace')
                errors.append(f'{model}: HTTP {e.code} {detail[:180]}')
            except Exception as e:
                errors.append(f'{model}: {str(e)[:180]}')
        raise RuntimeError('百炼语音合成失败：' + '；'.join(errors))
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
            raise RuntimeError('语音文件下载失败或为空')

    def _video_auth_header(self):
        auth = self.headers.get('X-Video-Auth', '').strip()
        if auth:
            return auth
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        token = query.get('token', [''])[0].strip()
        return f'Bearer {token}' if token else ''

    def _cache_video(self, video_url, auth_header=''):
        """Download the remote video once, then serve it as a local mp4.

        Browser video controls are much happier with a normal local file and
        proper byte-range responses than with a live upstream relay.
        """
        cache_dir = self._video_cache_dir()
        key = hashlib.sha256(video_url.encode('utf-8')).hexdigest()[:32]
        cache_path = os.path.join(cache_dir, f'{key}.mp4')
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 1024:
            return cache_path

        tmp_path = cache_path + '.part'
        ctx = ssl._create_unverified_context()
        base_headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'video/mp4,video/*;q=0.9,*/*;q=0.8',
        }
        attempts = [
            base_headers,
            {**base_headers, 'Range': 'bytes=0-'},
            {**base_headers, 'Referer': 'https://jimeng.jianying.com/'},
            {**base_headers, 'Referer': 'https://jimeng.jianying.com/', 'Origin': 'https://jimeng.jianying.com'},
        ]
        if auth_header:
            attempts = [
                {**base_headers, 'Authorization': auth_header},
                {**base_headers, 'Authorization': auth_header, 'Range': 'bytes=0-'},
                {**base_headers, 'Authorization': auth_header, 'Referer': 'https://jimeng.jianying.com/'},
                {**base_headers, 'Authorization': auth_header, 'Referer': 'https://jimeng.jianying.com/', 'Origin': 'https://jimeng.jianying.com'},
            ] + attempts

        last_error = None
        for headers in attempts:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                req = urllib.request.Request(video_url, headers=headers)
                with urllib.request.urlopen(req, context=ctx, timeout=180) as resp, open(tmp_path, 'wb') as f:
                    shutil.copyfileobj(resp, f, length=1024 * 1024)
                last_error = None
                break
            except urllib.error.HTTPError as e:
                last_error = f'HTTP Error {e.code}: {e.reason}'
                continue
            except Exception as e:
                last_error = str(e)
                continue
        if last_error:
            raise ValueError(f'All video download attempts failed. Last error: {last_error}')
        with open(tmp_path, 'rb') as f:
            head = f.read(256)
        if not self._looks_like_mp4(head):
            preview = head[:200].decode('utf-8', errors='replace')
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise ValueError(f'Upstream did not return an MP4 video. First bytes: {preview}')
        os.replace(tmp_path, cache_path)
        return cache_path

    def _looks_like_mp4(self, head):
        return b'ftyp' in head[:64] or head.startswith(b'\x00\x00\x00')

    def _serve_cached_video(self, cache_path):
        size = os.path.getsize(cache_path)
        ext = os.path.splitext(cache_path)[1].lower()
        content_type = 'audio/mpeg' if ext == '.mp3' else 'audio/aiff' if ext == '.aiff' else 'video/mp4'
        range_header = self.headers.get('Range')
        start, end = 0, size - 1
        status = 200

        if range_header and range_header.startswith('bytes='):
            status = 206
            range_value = range_header.split('=', 1)[1].split(',', 1)[0]
            start_text, end_text = range_value.split('-', 1)
            if start_text:
                start = int(start_text)
            if end_text:
                end = int(end_text)
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self._cors_headers()
                self.send_header('Content-Range', f'bytes */{size}')
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(status)
        self._cors_headers()
        self.send_header('Content-Type', content_type)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()

        with open(cache_path, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_html(self):
        try:
            with open(HTML_FILE, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, 'index.html not found')

    def _admin_page(self):
        html = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI内容成片工作台 - 管理后台</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:0;background:#f7f7f5;color:#1f2937}
.wrap{max-width:1280px;margin:0 auto;padding:28px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
.admin-shell{display:grid;grid-template-columns:220px minmax(0,1fr);gap:16px;align-items:start}
.admin-nav{position:sticky;top:18px;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:10px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.nav-btn{width:100%;display:flex;align-items:center;gap:8px;margin:4px 0;background:transparent;color:#374151;text-align:left;border-radius:8px}
.nav-btn.active{background:#4f46e5;color:#fff}.admin-section{display:none}.admin-section.active{display:block}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:18px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
input,select,textarea{padding:9px 10px;border:1px solid #d1d5db;border-radius:8px;font-size:14px}button{border:0;border-radius:8px;padding:9px 12px;background:#4f46e5;color:#fff;font-weight:700;cursor:pointer}
button.gray{background:#e5e7eb;color:#111827}button.red{background:#dc2626}.grid{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}.grid input{width:100%;box-sizing:border-box}
.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.field{display:grid;gap:6px}.field label{font-size:13px;color:#374151;font-weight:700}.field input,.field select{width:100%;box-sizing:border-box}.span2{grid-column:1/-1}
table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #eee;padding:9px;font-size:13px}th{color:#6b7280}.muted{color:#6b7280;font-size:13px}.pill{padding:3px 8px;border-radius:99px;font-size:12px;background:#eef2ff;color:#4338ca}.disabled{background:#fee2e2;color:#991b1b}
.login{max-width:360px;margin:12vh auto}.actions{display:flex;gap:6px;flex-wrap:wrap}
@media(max-width:760px){.settings-grid,.grid,.admin-shell{grid-template-columns:1fr}.wrap{padding:16px}.top{align-items:flex-start;gap:12px;flex-direction:column}.admin-nav{position:static}}
</style></head><body>
<div class="wrap">
  <div class="top"><div><h2>管理后台</h2><div class="muted">创建客户账号、设置免费次数、维护系统模型与素材策略。</div></div><div class="actions"><button class="gray" onclick="window.open('/','_blank')">打开客户端</button><button class="gray" onclick="logout()">退出</button></div></div>
  <div id="login" class="card login" style="display:none">
    <h3>管理员登录</h3>
    <p><input id="email" placeholder="账号" style="width:100%;box-sizing:border-box" value="admin"></p>
    <p><input id="password" type="password" placeholder="密码" style="width:100%;box-sizing:border-box" value=""></p>
    <button onclick="login()">登录</button><span class="muted" id="loginMsg"></span>
  </div>
  <div id="app" style="display:none">
    <div class="admin-shell">
      <aside class="admin-nav">
        <button class="nav-btn active" id="navSettings" onclick="showAdminSection('settings')">⚙️ 系统配置</button>
        <button class="nav-btn" id="navAccounts" onclick="showAdminSection('accounts')">👤 账号管理</button>
      </aside>
      <main>
    <section class="admin-section active" id="sectionSettings">
    <div class="card">
      <h3>系统配置</h3>
      <div class="settings-grid">
        <div class="field"><label>文案模型服务商</label><select id="cfgTextProviderPreset"><option value="deepseek">DeepSeek</option><option value="qwen">阿里百炼 / 千问 Qwen</option><option value="custom">自定义 OpenAI 兼容接口</option></select></div>
        <div class="field"><label>文案模型</label><input id="cfgTextModel" placeholder="deepseek-chat / qwen-plus"></div>
        <div class="field span2"><label>文案 API 地址</label><input id="cfgTextUrl" placeholder="https://api.deepseek.com/v1/chat/completions"></div>
        <div class="field span2"><label>文案 API Key</label><input id="cfgTextKey" type="password" placeholder="sk-..."></div>
        <div class="field"><label>视觉模型</label><input id="cfgVisionModel" placeholder="qwen3.6-35b-a3b"></div>
        <div class="field"><label>图生视频模型</label><select id="cfgI2vModel"><option value="wan2.7-i2v">wan2.7-i2v</option><option value="happyhorse-1.0-i2v">happyhorse-1.0-i2v</option><option value="wan2.6-i2v">wan2.6-i2v（备用）</option></select></div>
        <div class="field span2"><label>视觉模型 API Key（留空复用百炼 Key）</label><input id="cfgVisionKey" type="password" placeholder="sk-..."></div>
        <div class="field span2"><label>视觉模型 API 地址</label><input id="cfgVisionUrl" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"></div>
        <div class="field span2"><label>百炼 Key（生图、图生视频、配音）</label><input id="cfgWanxKey" type="password" placeholder="sk-..."></div>
        <div class="field"><label>图片服务商</label><select id="cfgImgProvider"><option value="bailian">阿里云百炼</option><option value="agnes">Agnes AI</option><option value="jimeng">即梦 AI</option></select></div>
        <div class="field"><label>图片模型</label><input id="cfgImgModel" placeholder="wanx-v1"></div>
        <div class="field"><label>图片尺寸</label><select id="cfgImgSize"><option value="1024*1024">1024×1024</option><option value="720*1280">720×1280</option><option value="1280*720">1280×720</option></select></div>
        <div class="field"><label>默认音色</label><select id="cfgVoice"><option value="longanyang">龙安阳（稳重男声）</option></select></div>
        <div class="field"><label>公共素材源</label><select id="cfgPublicMaterialProvider"><option value="off">关闭：仅本地素材库</option><option value="system_cn">系统公共素材库（预留）</option><option value="domestic_api">国内素材 API（预留）</option><option value="pexels">Pexels 海外备用</option></select></div>
        <div class="field"><label>公共素材策略</label><select id="cfgPublicMaterialPolicy"><option value="local_first">本地优先，公共素材只补缺口</option><option value="strict_cn">国内企业风格优先</option><option value="disabled_in_mix">混剪只用本地素材</option></select></div>
        <label class="field span2" style="display:flex;align-items:center;gap:8px"><input id="cfgOssEnabled" type="checkbox" style="width:auto"> <span>启用阿里云 OSS（让本地素材可用于图生视频）</span></label>
        <div class="field"><label>OSS AccessKey ID</label><input id="cfgOssAccessKeyId" placeholder="LTAI..."></div>
        <div class="field"><label>OSS AccessKey Secret</label><input id="cfgOssAccessKeySecret" type="password" placeholder="AccessKey Secret"></div>
        <div class="field"><label>OSS Bucket</label><input id="cfgOssBucket" placeholder="your-bucket"></div>
        <div class="field"><label>OSS Region</label><input id="cfgOssRegion" placeholder="oss-cn-hangzhou"></div>
        <div class="field span2"><label>OSS Endpoint</label><input id="cfgOssEndpoint" placeholder="oss-cn-hangzhou.aliyuncs.com"></div>
        <div class="field"><label>OSS 路径前缀</label><input id="cfgOssPrefix" placeholder="agent-workflow"></div>
        <div class="field"><label>签名 URL 有效期（秒）</label><input id="cfgOssUrlExpires" placeholder="7200"></div>
        <label class="field span2" style="display:flex;align-items:center;gap:8px"><input id="cfgLocalStoryboardMode" type="checkbox" style="width:auto"> <span>跳过文案模型，使用本地测试分镜</span></label>
      </div>
      <div class="actions" style="margin-top:12px"><button onclick="saveSettings()">保存系统配置</button><button class="gray" onclick="loadSettings()">重新读取</button><span class="muted" id="settingsMsg"></span></div>
      <div class="muted" style="margin-top:8px">客户端会在登录后自动读取这里的配置；文生视频 t2v 已从客户端配置中移除。</div>
    </div>
    </section>
    <section class="admin-section" id="sectionAccounts">
    <div class="card">
      <h3>新建账号</h3>
      <div class="grid">
        <input id="newEmail" placeholder="账号/邮箱">
        <input id="newPassword" placeholder="初始密码">
        <input id="newNickname" placeholder="昵称/客户名">
        <input id="newCredits" type="number" placeholder="免费次数" value="10">
        <input id="newNotes" placeholder="备注">
        <button onclick="createUser()">创建</button>
      </div>
      <div class="muted" id="createMsg"></div>
    </div>
    <div class="card"><h3>账号列表</h3><div id="users"></div></div>
    <div class="card"><h3>最近使用记录</h3><div id="usage"></div></div>
    </section>
      </main>
    </div>
  </div>
</div>
<script>
let token=localStorage.getItem('agentflow_auth_token')||'';
const el=id=>document.getElementById(id);
const h=()=>({'Content-Type':'application/json','X-Auth-Token':token});
async function api(url,opt={}){const r=await fetch(url,{...opt,headers:{...h(),...(opt.headers||{})}});const d=await r.json().catch(()=>({}));if(!r.ok||d.ok===false)throw new Error(d.error||'请求失败');return d}
async function login(){try{const d=await api('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:el('email').value,password:el('password').value})});token=d.token;localStorage.setItem('agentflow_auth_token',token);await boot()}catch(e){el('loginMsg').textContent=' '+e.message}}
async function logout(){localStorage.removeItem('agentflow_auth_token');token='';location.reload()}
function showAdminSection(name){const isSettings=name==='settings';el('sectionSettings').classList.toggle('active',isSettings);el('sectionAccounts').classList.toggle('active',!isSettings);el('navSettings').classList.toggle('active',isSettings);el('navAccounts').classList.toggle('active',!isSettings)}
async function boot(){try{const me=await api('/auth/me');if(me.user.role!=='admin')throw new Error('不是管理员账号');el('login').style.display='none';el('app').style.display='block';await loadSettings();await loadUsers();await loadUsage()}catch(e){el('app').style.display='none';el('login').style.display='block'}}
function fillSettings(s){el('cfgTextProviderPreset').value=s.textProviderPreset||'deepseek';el('cfgTextUrl').value=s.textUrl||'';el('cfgTextKey').value=s.textKey||'';el('cfgTextModel').value=s.textModel||'deepseek-chat';el('cfgVisionKey').value=s.visionKey||'';el('cfgVisionModel').value=s.visionModel||'qwen3.6-35b-a3b';el('cfgVisionUrl').value=s.visionUrl||'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions';el('cfgWanxKey').value=s.wanxKey||'';el('cfgImgProvider').value=s.imgProvider||'bailian';el('cfgImgModel').value=s.imgModel||'wanx-v1';el('cfgImgSize').value=s.imgSize||'1024*1024';el('cfgI2vModel').value=s.i2vModel==='wan2.6-i2v'?'wan2.7-i2v':(s.i2vModel||'wan2.7-i2v');el('cfgVoice').value=s.voice||'longanyang';el('cfgPublicMaterialProvider').value=s.publicMaterialProvider||'off';el('cfgPublicMaterialPolicy').value=s.publicMaterialPolicy||'local_first';el('cfgOssEnabled').checked=!!s.ossEnabled;el('cfgOssAccessKeyId').value=s.ossAccessKeyId||'';el('cfgOssAccessKeySecret').value=s.ossAccessKeySecret||'';el('cfgOssBucket').value=s.ossBucket||'';el('cfgOssRegion').value=s.ossRegion||'';el('cfgOssEndpoint').value=s.ossEndpoint||'';el('cfgOssPrefix').value=s.ossPrefix||'agent-workflow';el('cfgOssUrlExpires').value=s.ossUrlExpires||'7200';el('cfgLocalStoryboardMode').checked=!!s.localStoryboardMode}
function readSettings(){return{textProviderPreset:el('cfgTextProviderPreset').value,textUrl:el('cfgTextUrl').value,textKey:el('cfgTextKey').value,textModel:el('cfgTextModel').value,visionKey:el('cfgVisionKey').value,visionModel:el('cfgVisionModel').value,visionUrl:el('cfgVisionUrl').value,wanxKey:el('cfgWanxKey').value,imgProvider:el('cfgImgProvider').value,imgModel:el('cfgImgModel').value,imgSize:el('cfgImgSize').value,i2vModel:el('cfgI2vModel').value,voice:el('cfgVoice').value,publicMaterialProvider:el('cfgPublicMaterialProvider').value,publicMaterialPolicy:el('cfgPublicMaterialPolicy').value,ossEnabled:el('cfgOssEnabled').checked,ossAccessKeyId:el('cfgOssAccessKeyId').value,ossAccessKeySecret:el('cfgOssAccessKeySecret').value,ossBucket:el('cfgOssBucket').value,ossRegion:el('cfgOssRegion').value,ossEndpoint:el('cfgOssEndpoint').value,ossPrefix:el('cfgOssPrefix').value,ossUrlExpires:el('cfgOssUrlExpires').value,localStoryboardMode:el('cfgLocalStoryboardMode').checked}}
async function loadSettings(){try{const d=await api('/api/admin/settings');fillSettings(d.settings||{});el('settingsMsg').textContent='已读取'}catch(e){el('settingsMsg').textContent=e.message}}
async function saveSettings(){try{const d=await api('/api/admin/settings',{method:'POST',body:JSON.stringify({settings:readSettings()})});fillSettings(d.settings||{});el('settingsMsg').textContent='已保存，客户端刷新后生效'}catch(e){el('settingsMsg').textContent=e.message}}
async function createUser(){try{await api('/api/admin/users',{method:'POST',body:JSON.stringify({email:el('newEmail').value,password:el('newPassword').value,nickname:el('newNickname').value,credits:el('newCredits').value,notes:el('newNotes').value})});el('createMsg').textContent='创建成功';el('newEmail').value='';el('newPassword').value='';el('newNickname').value='';el('newNotes').value='';await loadUsers()}catch(e){el('createMsg').textContent=e.message}}
async function setQuota(id){const v=prompt('设置总免费次数');if(v===null)return;await api('/api/admin/quota',{method:'POST',body:JSON.stringify({userId:id,totalCredits:Number(v)})});await loadUsers()}
async function setStatus(id,status){await api('/api/admin/status',{method:'POST',body:JSON.stringify({userId:id,status})});await loadUsers()}
async function setPassword(id){const p=prompt('输入新密码（至少6位）');if(!p)return;await api('/api/admin/password',{method:'POST',body:JSON.stringify({userId:id,password:p})});alert('密码已更新')}
async function loadUsers(){const d=await api('/api/admin/users');el('users').innerHTML='<table><thead><tr><th>账号</th><th>昵称</th><th>状态</th><th>次数</th><th>备注</th><th>操作</th></tr></thead><tbody>'+d.users.map(u=>`<tr><td>${esc(u.email)}</td><td>${esc(u.nickname)}</td><td><span class="pill ${u.status==='disabled'?'disabled':''}">${u.status}</span></td><td>${u.usedCredits}/${u.totalCredits}，剩余 ${u.remainingCredits}</td><td>${esc(u.notes)}</td><td class="actions"><button class="gray" onclick="setQuota(${u.id})">改次数</button><button class="gray" onclick="setPassword(${u.id})">改密码</button>${u.status==='active'?`<button class="red" onclick="setStatus(${u.id},'disabled')">禁用</button>`:`<button onclick="setStatus(${u.id},'active')">启用</button>`}</td></tr>`).join('')+'</tbody></table>'}
async function loadUsage(){const d=await api('/api/admin/usage');el('usage').innerHTML='<table><thead><tr><th>时间</th><th>账号</th><th>动作</th><th>扣次</th><th>任务</th></tr></thead><tbody>'+d.records.map(r=>`<tr><td>${esc(r.created_at)}</td><td>${esc(r.email)}</td><td>${esc(r.action)}</td><td>${r.credits_used}</td><td>${esc(r.task_title||'')}</td></tr>`).join('')+'</tbody></table>'}
function esc(s){return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
boot();
</script></body></html>'''
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method):
        """Forward request to the target API."""
        # /api/deepseek/... -> target URL encoded in path
        # Or use a header: X-Target-URL
        target_url = self.headers.get('X-Target-URL')
        if not target_url:
            self.send_error(400, 'Missing X-Target-URL header')
            return

        # Read request body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        proxy_method = self.headers.get('X-Proxy-Method', method).upper()
        if proxy_method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
            proxy_method = method
        if proxy_method == 'GET':
            body = None
        elif body and 'video-generation/video-synthesis' in target_url:
            body = self._normalize_i2v_proxy_body(body)

        # Forward auth header
        auth = self.headers.get('X-Auth-Header', '')
        dashscope_async = self.headers.get('X-DashScope-Async', '')

        req_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 AgentWorkflow/1.0',
        }
        if auth:
            req_headers['Authorization'] = auth
        if dashscope_async:
            req_headers['X-DashScope-Async'] = dashscope_async

        try:
            ctx = ssl._create_unverified_context()
            last_error = None
            resp = None
            for attempt in range(3):
                try:
                    req = urllib.request.Request(
                        target_url,
                        data=body,
                        headers=req_headers,
                        method=proxy_method,
                    )
                    # Bypass SSL verification for proxy forwarding
                    resp = urllib.request.urlopen(req, context=ctx, timeout=120)
                    break
                except urllib.error.HTTPError:
                    raise
                except Exception as retry_error:
                    last_error = retry_error
                    if attempt < 2:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise last_error

            self.send_response(resp.status)
            self._cors_headers()
            self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json'))
            self.end_headers()
            self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read()
            self.send_response(e.code)
            self._cors_headers()
            self.send_header('Content-Type', e.headers.get('Content-Type', 'application/json; charset=utf-8'))
            self.end_headers()
            self.wfile.write(error_body)
        except Exception as e:
            payload = {
                'error': {
                    'type': e.__class__.__name__,
                    'message': str(e),
                    'target_url': target_url,
                    'method': proxy_method,
                }
            }
            body_bytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(502)
            self._cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

    def _normalize_i2v_proxy_body(self, body):
        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception:
            return body
        model = str(payload.get('model') or payload.get('input', {}).get('model') or '').lower()
        if 'wan2.7' not in model:
            return body
        input_payload = payload.get('input')
        if not isinstance(input_payload, dict):
            return body
        changed = False
        media = input_payload.get('media')
        if isinstance(media, list):
            for item in media:
                if isinstance(item, dict) and item.get('type') == 'image':
                    item['type'] = 'first_frame'
                    changed = True
        elif isinstance(media, dict) and media.get('type') == 'image':
            media['type'] = 'first_frame'
            changed = True
        if changed:
            print('[proxy] normalized wan2.7-i2v media type image -> first_frame')
            return json.dumps(payload, ensure_ascii=False).encode('utf-8')
        return body

    def _publish_status(self):
        """GET /publish/status?task_id=xxx 查询发布任务状态"""
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        task_id = query.get('task_id', [None])[0]
        if not task_id:
            self._json_response(400, {'error': 'Missing task_id parameter'})
            return
        # 安全校验：只允许字母数字下划线连字符
        if not re.match(r'^[\w\-]+$', task_id):
            self._json_response(400, {'error': 'Invalid task_id'})
            return
        status_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'publish_tasks', f'{task_id}.json'
        )
        if not os.path.exists(status_file):
            self._json_response(404, {
                'task_id': task_id,
                'status': 'not_found',
                'message': '任务不存在或已过期'
            })
            return
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._json_response(200, data)
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _publish_douyin_draft(self):
        """POST /publish/douyin-draft  启动抖音草稿发布子进程"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
            payload = json.loads(raw.decode('utf-8'))
        except Exception as e:
            self._json_response(400, {'error': f'Invalid request body: {e}'})
            return

        video_path = payload.get('video_path', '')
        video_url = payload.get('video_url', '')
        title = payload.get('title', '')
        body = payload.get('body', '')
        hashtags = payload.get('hashtags', '')

        # 优先使用绝对路径；否则从 video_url 解析缓存文件路径
        if not video_path and video_url:
            from urllib.parse import urlparse, unquote
            parsed = urlparse(video_url)
            filename = os.path.basename(unquote(parsed.path))
            video_path = os.path.join(self._video_cache_dir(), filename)

        if not video_path or not os.path.exists(video_path):
            self._json_response(400, {
                'error': '视频文件不存在。请先生成并下载视频，或确认 video_path/video_url 正确。',
                'resolved_path': video_path
            })
            return

        # 生成任务 ID
        task_id = f'douyin_{int(time.time())}_{os.urandom(4).hex()}'
        script_dir = os.path.dirname(os.path.abspath(__file__))
        publisher_script = os.path.join(script_dir, 'douyin_publisher.py')

        # 使用系统 Python（Playwright 安装在其 site-packages 下）
        python_exe = '/usr/bin/python3'
        cmd = [
            python_exe, publisher_script,
            '--task-id', task_id,
            '--video-path', video_path,
            '--title', title,
            '--body', body,
            '--hashtags', hashtags,
        ]

        try:
            debug_dir = os.path.join(script_dir, 'publish_debug')
            os.makedirs(debug_dir, exist_ok=True)
            log_path = os.path.join(debug_dir, f'{task_id}.log')
            log_file = open(log_path, 'a', encoding='utf-8')
            subprocess.Popen(
                cmd,
                cwd=script_dir,
                stdout=log_file,
                stderr=log_file,
            )
            log_file.close()
        except Exception as e:
            try:
                log_file.close()
            except Exception:
                pass
            self._json_response(500, {'error': f'无法启动发布进程: {e}'})
            return

        self._json_response(200, {
            'task_id': task_id,
            'status': 'queued',
            'status_endpoint': f'/publish/status?task_id={task_id}',
            'log_path': log_path,
        })

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Auth-Token, X-Target-URL, X-Proxy-Method, X-Auth-Header, X-DashScope-Async, X-Video-Auth')

    def log_message(self, format, *args):
        # Quieter logging
        if '/api/' in str(args):
            print(f"  -> Proxy: {args[0]}")
        else:
            pass  # Suppress default logs


if __name__ == '__main__':
    if os.environ.get('KILL_EXISTING_PORT') == '1':
        os.system(f'lsof -ti:{PORT} | xargs kill -9 2>/dev/null')

    print(f"""
╔══════════════════════════════════════════╗
║   🤖 多Agent协同工作流 — 代理服务器       ║
║                                          ║
║   监听地址: {HOST}:{PORT}
║   本地打开: http://localhost:{PORT}
║   按 Ctrl+C 停止服务器                     ║
╚══════════════════════════════════════════╝
""")
    server = http.server.ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n👋 Server stopped.')
        server.server_close()
