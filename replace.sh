#!/usr/bin/env bash
# 自动应用 replace.md 中描述的改造：备份并注入到源码（适用于容器内执行）
# 用法（容器内）:
#   chmod +x docker/replace.sh && ./docker/replace.sh
# 用法（主机执行到已运行容器）:
#   docker exec -it <container> bash -lc 'cd /app && chmod +x docker/replace.sh && ./docker/replace.sh'
# 连接到容器终端
#   curl -s https://raw.githubusercontent.com/OnlineMo/Goofish-Auto-reply-replace/refs/heads/main/replace.sh | bash

set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
cd "$PROJECT_ROOT"

echo "[apply_replace_md] project root: $PROJECT_ROOT"

TS=$(date +%Y%m%d_%H%M%S)

backup() {
  local f
  for f in "$@"; do
    if [ -f "$f" ]; then
      cp -a "$f" "${f}.bak.${TS}"
      echo "backup: $f -> ${f}.bak.${TS}"
    else
      echo "skip backup (not found): $f"
    fi
  done
}

# 备份原文件
backup db_manager.py cookie_manager.py Start.py requirements.txt

python3 - <<'PY'
import os, re, json, hashlib

root='.'
paths={'db':'db_manager.py','cm':'cookie_manager.py','start':'Start.py','req':'requirements.txt'}

def read(p):
    with open(p,'r',encoding='utf-8') as f: return f.read()
def write(p,s):
    with open(p,'w',encoding='utf-8',newline='') as f: f.write(s)

# 1) requirements.txt: 确保 pycryptodome 依赖
try:
    rp=paths['req']
    if os.path.exists(rp):
        txt=read(rp)
    else:
        txt=''
    if 'pycryptodome' not in txt:
        if not txt.endswith('\n'): txt+='\n'
        txt+='pycryptodome>=3.20.0\n'
        write(rp,txt)
        print('[+] requirements.txt: appended pycryptodome')
except Exception as e:
    print('[!] requirements.txt update skipped',e)

# 2) db_manager.py 注入: 建表 + 新方法
dp=paths['db']
txt=read(dp)

# 2.1 建表: encrypted_cookies
if 'encrypted_cookies' not in txt:
    m=re.search(r"cursor\\.execute\\('''\\s*CREATE TABLE IF NOT EXISTS system_settings[\\s\\S]*?'''\\)", txt)
    if m:
        ins = """
            # 存储按 uuid 归档的加密 cookie 内容
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS encrypted_cookies (
                uuid TEXT PRIMARY KEY,
                encrypted TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
"""
        txt = txt[:m.end()] + ins + txt[m.end():]
        print('[+] db_manager.py: inserted encrypted_cookies table')
    else:
        print('[!] db_manager.py: anchor not found for table, skipping create')

# 2.2 新方法: upsert_encrypted_cookie / get_encrypted_cookie
if 'def upsert_encrypted_cookie' not in txt:
    mm = re.search(r"\n\s+def export_backup\(", txt)
    methods = """
    def upsert_encrypted_cookie(self, uuid: str, encrypted: str) -> bool:
        \"\"\"新增/更新按 uuid 存储的加密 cookie 密文\"\"\"
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, '''
                INSERT INTO encrypted_cookies (uuid, encrypted)
                VALUES (?, ?)
                ON CONFLICT(uuid) DO UPDATE SET
                    encrypted = excluded.encrypted,
                    updated_at = CURRENT_TIMESTAMP
                ''', (uuid, encrypted))
                self.conn.commit()
                logger.info(f"加密Cookie密文已保存: uuid={uuid}, 长度={len(encrypted)}")
                return True
            except Exception as e:
                logger.error(f"保存加密Cookie密文失败: uuid={uuid}, 错误: {e}")
                self.conn.rollback()
                return False

    def get_encrypted_cookie(self, uuid: str) -> Optional[str]:
        \"\"\"读取按 uuid 存储的加密 cookie 密文\"\"\"
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, "SELECT encrypted FROM encrypted_cookies WHERE uuid = ?", (uuid,))
                row = cursor.fetchone()
                return row[0] if row else None
            except Exception as e:
                logger.error(f"读取加密Cookie密文失败: uuid={uuid}, 错误: {e}")
                return None

"""
    if mm:
        idx = mm.start()
        txt = txt[:idx] + methods + txt[idx:]
        print('[+] db_manager.py: added upsert/get encrypted cookie methods')
    else:
        mm2=re.search(r"\n# 全局单例", txt)
        if mm2:
            idx=mm2.start()
            txt = txt[:idx] + methods + txt[idx:]
            print('[+] db_manager.py: added methods before singleton')
        else:
            print('[!] db_manager.py: anchor not found for methods insertion')

write(dp, txt)

# 3) cookie_manager.py 注入: imports + ApiCrypto + 类方法
cp=paths['cm']
cm=read(cp)

# 3.1 imports
def add_import_once(src, imp_line):
    if imp_line in src: return src, False
    # 插到 from db_manager import db_manager 后面
    lines=src.splitlines()
    insert_at=None
    for i, line in enumerate(lines[:40]):
        if line.strip().startswith('from db_manager import db_manager'):
            insert_at=i+1
            break
    if insert_at is None:
        # 若找不到，就在首行后插入
        insert_at=1
    lines.insert(insert_at, imp_line)
    return '\n'.join(lines)+'\n', True

for imp in ('import base64','import hashlib','import json'):
    cm, did = add_import_once(cm, imp)
    if did: print(f'[+] cookie_manager.py: import {imp}')

# 3.2 ApiCrypto（放在 CookieManager 之前）
if 'class ApiCrypto' not in cm:
    m = re.search(r"\nclass CookieManager\b", cm)
    if m:
        idx=m.start()
        crypto = """
class ApiCrypto:
    \"\"\"/update 与 /get/:uuid API 的加解密工具（AES-128-ECB + PKCS7）\"\"\"
    @staticmethod
    def _derive_key(uuid: str, password: str) -> bytes:
        md5_hex = hashlib.md5((uuid + password).encode('utf-8')).hexdigest()
        return md5_hex[:16].encode('utf-8')
    @staticmethod
    def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
        pad_len = block_size - (len(data) % block_size)
        return data + bytes([pad_len]) * pad_len
    @staticmethod
    def _pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
        if not data:
            raise ValueError("Invalid padding (empty data)")
        pad_len = data[-1]
        if pad_len <= 0 or pad_len > block_size or data[-pad_len:] != bytes([pad_len]) * pad_len:
            raise ValueError("Invalid PKCS7 padding")
        return data[:-pad_len]
    @staticmethod
    def encrypt(plaintext: str, uuid: str, password: str) -> str:
        try:
            from Crypto.Cipher import AES
        except Exception as e:
            raise RuntimeError("缺少依赖 pycryptodome，请安装后再试") from e
        key = ApiCrypto._derive_key(uuid, password)
        cipher = AES.new(key, AES.MODE_ECB)
        data = plaintext.encode('utf-8')
        ct = cipher.encrypt(ApiCrypto._pkcs7_pad(data))
        return base64.b64encode(ct).decode('utf-8')
    @staticmethod
    def decrypt(encrypted_b64: str, uuid: str, password: str) -> str:
        try:
            from Crypto.Cipher import AES
        except Exception as e:
            raise RuntimeError("缺少依赖 pycryptodome，请安装后再试") from e
        key = ApiCrypto._derive_key(uuid, password)
        cipher = AES.new(key, AES.MODE_ECB)
        ct = base64.b64decode(encrypted_b64)
        pt = cipher.decrypt(ct)
        pt = ApiCrypto._pkcs7_unpad(pt)
        return pt.decode('utf-8')

"""
        cm = cm[:idx] + crypto + cm[idx:]
        print('[+] cookie_manager.py: inserted ApiCrypto')
    else:
        print('[!] cookie_manager.py: anchor not found for ApiCrypto')

# 3.3 在 CookieManager 类中追加方法
if 'def save_encrypted_cookie_blob' not in cm:
    mm = re.search(r"\n\s+def get_auto_confirm_setting\(", cm)
    methods = """
    def save_encrypted_cookie_blob(self, uuid: str, encrypted: str) -> bool:
        \"\"\"存储客户端上送的加密cookie密文（/update）\"\"\"
        if not uuid or not encrypted:
            raise ValueError("uuid 与 encrypted 不能为空")
        return db_manager.upsert_encrypted_cookie(uuid, encrypted)

    def get_cookie_blob(self, uuid: str, password: Optional[str] = None) -> Optional[dict]:
        \"\"\"读取密文或尝试解密（/get/:uuid）\"\"\"
        encrypted = db_manager.get_encrypted_cookie(uuid)
        if encrypted is None:
            return None
        if not password:
            return {"uuid": uuid, "encrypted": encrypted}
        try:
            plaintext = ApiCrypto.decrypt(encrypted, uuid, password)
        except Exception as e:
            raise ValueError(f"解密失败: {e}")
        try:
            parsed = json.loads(plaintext)
            return {"uuid": uuid, "content": parsed}
        except Exception:
            return {"uuid": uuid, "content": plaintext}

"""
    insert_idx=None
    if mm:
        start=mm.end()
        mm2=re.search(r"\n\s{4}def\s+\w+\(", cm[start:])
        end_of_class_marker=re.search(r"\nmanager\s*:\s*Optional\[CookieManager\]", cm[start:])
        if mm2:
            insert_idx = start + mm2.start()
        elif end_of_class_marker:
            insert_idx = start + end_of_class_marker.start()
        else:
            insert_idx = start
    else:
        mm3 = re.search(r"\nmanager\s*:\s*Optional\[CookieManager\]", cm)
        if mm3:
            insert_idx = mm3.start()
    if insert_idx is not None:
        cm = cm[:insert_idx] + methods + cm[insert_idx:]
        print('[+] cookie_manager.py: added class methods save_encrypted_cookie_blob/get_cookie_blob')
    else:
        print('[!] cookie_manager.py: could not locate insertion point for class methods')

write(cp, cm)

# 4) Start.py: 引入 CookieCloud 刷新循环（环境变量控制）
sp=paths['start']
st=read(sp)

# 4.1 imports
def ensure_import(st, imp):
    if imp in st: return st, False
    return st.replace('from loguru import logger', f'from loguru import logger\n{imp}'), True

for imp in ('import aiohttp','import hashlib','import base64','import json'):
    st, did = ensure_import(st, imp)
    if did: print(f'[+] Start.py: import {imp}')

# 4.2 帮助函数
if 'def cookiecloud_decrypt(' not in st:
    helper = """
from Crypto.Cipher import AES

def _evp_bytes_to_key_md5(passphrase: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16):
    d = b""
    last = b""
    while len(d) < (key_len + iv_len):
        last = hashlib.md5(last + passphrase + salt).digest()
        d += last
    return d[:key_len], d[key_len:key_len + iv_len]

def _pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data or len(data) % block_size != 0:
        raise ValueError("Invalid block size or empty data")
    pad_len = data[-1]
    if pad_len <= 0 or pad_len > block_size or data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("Invalid PKCS7 padding")
    return data[:-pad_len]

def cookiecloud_decrypt(encrypted_b64: str, uuid: str, password: str) -> str:
    raw = base64.b64decode(encrypted_b64)
    if len(raw) < 16 or raw[:8] != b"Salted__":
        raise ValueError("Invalid CookieCloud ciphertext (missing 'Salted__')")
    salt = raw[8:16]
    ciphertext = raw[16:]
    passphrase = hashlib.md5((uuid + '-' + password).encode('utf-8')).hexdigest()[:16].encode('utf-8')
    key, iv = _evp_bytes_to_key_md5(passphrase, salt, key_len=32, iv_len=16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(ciphertext)
    plaintext = _pkcs7_unpad(decrypted).decode('utf-8')
    return plaintext

def cookiecloud_cookie_string_from_payload(payload: dict) -> str:
    cookie_pairs = []
    cookie_data = payload.get('cookie_data', {}) or {}
    for _, items in cookie_data.items():
        for item in items or []:
            name = item.get('name')
            value = item.get('value')
            if name and value is not None:
                cookie_pairs.append(f"{name}={value}")
    seen = set()
    dedup = []
    for kv in reversed(cookie_pairs):
        k = kv.split('=', 1)[0]
        if k not in seen:
            seen.add(k)
            dedup.append(kv)
    dedup.reverse()
    return '; '.join(dedup)

async def _cookiecloud_refresh_loop(manager):
    host = os.getenv('COOKIE_CLOUD_HOST', '').strip().rstrip('/')
    uuid = os.getenv('COOKIE_CLOUD_UUID', '').strip()
    pwd = os.getenv('COOKIE_CLOUD_PASSWORD', '').strip()
    if not host or not uuid or not pwd:
        logger.info("CookieCloud 未配置环境变量，保持使用现有 Cookie")
        return
    try:
        interval = int(os.getenv('COOKIE_CLOUD_REFRESH_SECONDS', '600'))
        if interval <= 0:
            interval = 600
    except Exception:
        interval = 600

    async def _pick_target_cookie_id() -> str:
        target = os.getenv('COOKIE_CLOUD_TARGET_COOKIE_ID', '').strip()
        if target:
            return target
        if 'default' in cm.manager.cookies:
            return 'default'
        if cm.manager.cookies:
            return next(iter(cm.manager.cookies.keys()))
        return 'cookiecloud'

    logger.info(f"CookieCloud 刷新任务已启动，host={host}, uuid={uuid}, interval={interval}s")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                url = f"{host}/get/{uuid}"
                async with session.get(url, timeout=20) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning(f"CookieCloud 获取失败: {resp.status}, {text[:200]}")
                    else:
                        data = await resp.json(content_type=None)
                        encrypted = (data or {}).get('encrypted')
                        if not encrypted:
                            logger.warning("CookieCloud 返回数据不含 encrypted 字段")
                        else:
                            plaintext = cookiecloud_decrypt(encrypted, uuid, pwd)
                            try:
                                parsed = json.loads(plaintext)
                            except Exception:
                                parsed = {'cookie_data': {}, 'raw': plaintext}
                            cookie_str = cookiecloud_cookie_string_from_payload(parsed)
                            tcid = await _pick_target_cookie_id()
                            if tcid in cm.manager.cookies:
                                cm.manager.update_cookie(tcid, cookie_str)
                                logger.info(f"CookieCloud 已更新账号 {tcid} 的 Cookie（长度={len(cookie_str)}）")
                            else:
                                cm.manager.add_cookie(tcid, cookie_str)
                                logger.info(f"CookieCloud 已新增账号 {tcid} 并写入 Cookie（长度={len(cookie_str)}）")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"CookieCloud 刷新异常: {e}")
            finally:
                await asyncio.sleep(interval)
"""
    mm=re.search(r"\nasync def main\(", st)
    if mm:
        st = st[:mm.start()] + '\n' + helper + '\n' + st[mm.start():]
        print('[+] Start.py: inserted CookieCloud helper functions')
    else:
        print('[!] Start.py: cannot find main() to insert helpers')

# 4.3 在 main() 中启动刷新任务
if 'loop.create_task(_cookiecloud_refresh_loop(manager))' not in st:
    anchor = 'print("启动 API 服务线程...")'
    if anchor in st:
        st = st.replace(anchor, 'loop.create_task(_cookiecloud_refresh_loop(manager))\n    ' + anchor, 1)
        print('[+] Start.py: scheduled CookieCloud refresh loop')
    else:
        print('[!] Start.py: anchor not found to schedule refresh loop')

write(sp, st)

print('[*] apply_replace_md completed.')
PY

echo "[apply_replace_md] done."
