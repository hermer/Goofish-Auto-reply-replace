# Goofish-Auto-reply-replace
修改 https://github.com/zhinianboke/xianyu-auto-reply 的docker，使其支持 https://github.com/easychen/CookieCloud

# 修改内容
# /update 与 /get/:uuid API 支持 - 后端方法与加解密实现

本文提供对 [db_manager.py](db_manager.py) 与 [cookie_manager.py](cookie_manager.py) 的增量修改，新增支持如下 API 的服务端方法（仅方法与存储，不改动路由层）：
- Upload: POST /update，参数 uuid、encrypted（客户端本地加密后的字符串）
- Download: GET/POST /get/:uuid，参数 password（可选；缺省则返回密文，有则尝试解密并返回内容）

约定
- 加密算法：AES-128-ECB + PKCS7 填充，密文采用 Base64 字符串
- 密钥派生：key = md5(uuid + password).hexdigest().substr(0, 16)（16 个 ASCII 字节）
- Upload：服务端不参与加密，仅存储密文
- Download：若提供 password 则使用上述规则解密

提示（前端 CryptoJS 对齐）
- 需使用 ECB 模式和 Pkcs7 填充，且将 16 个字符的 key 按 UTF-8 作为原始字节使用（不是 Hex 字节）
- 示例（CryptoJS）：
  ```js
  const data = JSON.stringify(cookies);
  const keyStr = CryptoJS.MD5(uuid + password).toString().substr(0, 16);
  const key = CryptoJS.enc.Utf8.parse(keyStr);
  const encrypted = CryptoJS.AES.encrypt(data, key, {
    mode: CryptoJS.mode.ECB,
    padding: CryptoJS.pad.Pkcs7
  }).toString();
  ```

---

## 一、数据库层新增密文表与方法

在 [python.def DBManager.init_db()](db_manager.py:67) 的建表区域追加密文归档表（紧随现有 CREATE TABLE 段落之后均可）：

```python
# 存储按 uuid 归档的加密 cookie 内容
cursor.execute('''
CREATE TABLE IF NOT EXISTS encrypted_cookies (
    uuid TEXT PRIMARY KEY,
    encrypted TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')
```

新增两个方法：
- [python.def DBManager.upsert_encrypted_cookie()](db_manager.py:2050)
- [python.def DBManager.get_encrypted_cookie()](db_manager.py:2068)

将如下方法添加到 DBManager 类（建议放在“Cookie操作”小节之后，或任意方法段末尾，位于类结束前）：

```python
def upsert_encrypted_cookie(self, uuid: str, encrypted: str) -> bool:
    """
    新增/更新按 uuid 存储的加密 cookie 密文
    """
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
    """
    读取按 uuid 存储的加密 cookie 密文
    """
    with self.lock:
        try:
            cursor = self.conn.cursor()
            self._execute_sql(cursor, "SELECT encrypted FROM encrypted_cookies WHERE uuid = ?", (uuid,))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.error(f"读取加密Cookie密文失败: uuid={uuid}, 错误: {e}")
            return None
```

---

## 二、业务层新增加解密工具与 API 支撑方法

在 [cookie_manager.py](cookie_manager.py) 中：
1) 顶部 imports 追加（不影响现有逻辑）：
```python
import base64
import hashlib
import json
```
2) 在文件内添加一个内部加解密工具类（类或模块内函数均可，推荐类以便封装）。建议置于类 CookieManager 定义之前：

- [python.def ApiCrypto._derive_key()](cookie_manager.py:20)
- [python.def ApiCrypto.encrypt()](cookie_manager.py:32)
- [python.def ApiCrypto.decrypt()](cookie_manager.py:52)

```python
class ApiCrypto:
    """
    /update 与 /get/:uuid API 的加解密工具（AES-128-ECB + PKCS7）
    依赖: pycryptodome (from Crypto.Cipher import AES)
    """
    @staticmethod
    def _derive_key(uuid: str, password: str) -> bytes:
        # 取 md5(uuid+password) 的十六进制字符串前16位，按 ASCII 作为AES-128 key
        md5_hex = hashlib.md5((uuid + password).encode('utf-8')).hexdigest()
        return md5_hex[:16].encode('utf-8')  # 16 bytes

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
        """
        仅供调试/校验；正式 Upload 由客户端完成
        """
        try:
            from Crypto.Cipher import AES  # pycryptodome
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
            from Crypto.Cipher import AES  # pycryptodome
        except Exception as e:
            raise RuntimeError("缺少依赖 pycryptodome，请安装后再试") from e

        key = ApiCrypto._derive_key(uuid, password)
        cipher = AES.new(key, AES.MODE_ECB)
        ct = base64.b64decode(encrypted_b64)
        pt = cipher.decrypt(ct)
        pt = ApiCrypto._pkcs7_unpad(pt)
        return pt.decode('utf-8')
```

3) 在 [python.def CookieManager](cookie_manager.py:10) 类内新增对外方法（便于路由层直接调用）：
- [python.def CookieManager.save_encrypted_cookie_blob()](cookie_manager.py:190)
- [python.def CookieManager.get_cookie_blob()](cookie_manager.py:203)

```python
def save_encrypted_cookie_blob(self, uuid: str, encrypted: str) -> bool:
    """
    存储客户端上送的加密cookie密文（/update）
    """
    if not uuid or not encrypted:
        raise ValueError("uuid 与 encrypted 不能为空")
    return db_manager.upsert_encrypted_cookie(uuid, encrypted)

def get_cookie_blob(self, uuid: str, password: Optional[str] = None) -> Optional[dict]:
    """
    读取密文或尝试解密（/get/:uuid）
    - 无 password: 返回 {"uuid": uuid, "encrypted": <密文>}
    - 有 password: 解密并返回 {"uuid": uuid, "content": <原文JSON或字符串>}
      解密失败抛出 ValueError
    """
    encrypted = db_manager.get_encrypted_cookie(uuid)
    if encrypted is None:
        return None

    if not password:  # 仅返回密文
        return {"uuid": uuid, "encrypted": encrypted}

    # 尝试解密
    try:
        plaintext = ApiCrypto.decrypt(encrypted, uuid, password)
    except Exception as e:
        # 与路由层约定：密码错误或密文不匹配时抛错，由路由层返回 400/401
        raise ValueError(f"解密失败: {e}")

    # 尝试解析 JSON（若不是 JSON 也直接返回字符串）
    try:
        parsed = json.loads(plaintext)
        return {"uuid": uuid, "content": parsed}
    except Exception:
        return {"uuid": uuid, "content": plaintext}
```

---

## 三、路由层（参考用法，非本次必须改动）

在 FastAPI 路由中可直接使用以上方法（示例仅说明调用形态）：
```python
# POST /update
payload = await request.json()
ok = cookie_manager.manager.save_encrypted_cookie_blob(payload["uuid"], payload["encrypted"])
return {"success": ok}

# GET/POST /get/:uuid
uuid = path_param_uuid
password = request.query_params.get("password") or (await request.json()).get("password")
result = cookie_manager.manager.get_cookie_blob(uuid, password)
if result is None:
    raise HTTPException(status_code=404, detail="uuid not found")
return result
```

---

## 四、依赖与兼容

- 新增依赖：pycryptodome（用于 AES）
  - requirements.txt 增加：`pycryptodome>=3.20.0`
- 若运行环境暂未安装，可先本地测试安装：
  - `pip install pycryptodome`

---

## 五、自检清单（本地验证要点）

- 表存在：`SELECT name FROM sqlite_master WHERE type='table' AND name='encrypted_cookies';`
- 上传存储：调用 `save_encrypted_cookie_blob(uuid, encrypted)` 返回 True
- 下载返回密文：调用 `get_cookie_blob(uuid, None)` 返回包含 encrypted
- 下载解密成功：调用 `get_cookie_blob(uuid, password)` 返回 content（字典或字符串）
- 下载解密失败：密码不匹配时抛出 ValueError（由路由层映射到 400/401）
---
## 六、基于 CookieCloud 的环境变量刷新策略（不改路由，启动后自动生效）

目标
- 通过环境变量读取 CookieCloud 的服务器、UUID、密码与刷新周期；
- 若未配置则继续使用原有本地/DB 中的 Cookie；
- 若已配置，则定期从远端拉取加密数据，解密得到 cookies 并覆盖指定账号的 Cookie 值；
- 仅文档与代码片段，方便增量合入，无需改变现有 API。

注意：CookieCloud 的加解密与第 1~5 节中上传/下载 API 的算法不同
- CookieCloud 浏览器端使用的是 CryptoJS 的“口令(passphrase)模式”AES-CBC，密文以 "Salted__" 开头，包含随机 salt，需要 OpenSSL EVP_BytesToKey 派生 key/iv；
- 口令为 the_key = MD5(uuid + '-' + password).substring(0,16) 的 16 个字符（作为“口令字符串”，不是直接作为 key 字节）；
- 本节实现按 CookieCloud 官方示例与 Go/Deno 参考文档对应的“Salted__ + EVP_BytesToKey + AES-CBC + PKCS7”解密。

环境变量
- COOKIE_CLOUD_HOST: 远端服务地址（如 https://cookiecloud.example.com），不带尾随斜杠
- COOKIE_CLOUD_UUID: CookieCloud UUID
- COOKIE_CLOUD_PASSWORD: CookieCloud 密码
- COOKIE_CLOUD_REFRESH_SECONDS: 刷新间隔（秒），默认 600
- COOKIE_CLOUD_TARGET_COOKIE_ID: 要覆盖的账号ID，默认取顺序：default → 任意已有账号 → 'cookiecloud'（新建）

依赖
- [requirements.txt](requirements.txt) 确保存在（如未安装）：aiohttp、pycryptodome
  - aiohttp 已在项目其它模块中引用，一般已安装
  - pycryptodome>=3.20.0 已在前文第“四、依赖与兼容”说明

Start 入口新增刷新循环
1) 在 [Start.py](Start.py) 顶部 imports 追加（若已存在可忽略重复导入）：
```python
import aiohttp
import hashlib
import base64
import json
```

2) 在 [python.def main() 前增加以下工具函数](Start.py:1000)（放置于文件内任意函数区即可）：
```python
from Crypto.Cipher import AES

def _evp_bytes_to_key_md5(passphrase: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16):
    """
    OpenSSL EVP_BytesToKey (MD5) 实现，兼容 CryptoJS passphrase 模式
    返回: (key, iv)
    """
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
    """
    解密 CookieCloud 加密文本（CryptoJS.AES.encrypt(msg, passphrase) 风格）
    passphrase = MD5(uuid + '-' + password).substring(0,16)
    """
    raw = base64.b64decode(encrypted_b64)
    if len(raw) < 16 or raw[:8] != b"Salted__":
        raise ValueError("Invalid CookieCloud ciphertext (missing 'Salted__')")
    salt = raw[8:16]
    ciphertext = raw[16:]
    passphrase = hashlib.md5((uuid + '-' + password).encode('utf-8')).hexdigest()[:16].encode('utf-8')
    key, iv = _evp_bytes_to_key_md5(passphrase, salt, key_len=32, iv_len=16)  # AES-256-CBC
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(ciphertext)
    plaintext = _pkcs7_unpad(decrypted).decode('utf-8')
    return plaintext

def cookiecloud_cookie_string_from_payload(payload: dict) -> str:
    """
    将 { cookie_data, local_storage_data } 中的 cookie_data 扁平化为 'k=v; ...' 形式的 Cookie 字符串
    说明：为通用性，本实现合并所有域的 cookie 项；如需按域过滤，可自行在此处加筛选逻辑。
    """
    cookie_pairs = []
    cookie_data = payload.get('cookie_data', {}) or {}
    # cookie_data: { domain: [ {name, value, ...}, ... ], ... }
    for _, items in cookie_data.items():
        for item in items or []:
            name = item.get('name')
            value = item.get('value')
            if name and value is not None:
                cookie_pairs.append(f"{name}={value}")
    # 去重保持后者优先
    seen = set()
    dedup = []
    for kv in reversed(cookie_pairs):
        k = kv.split('=', 1)[0]
        if k not in seen:
            seen.add(k)
            dedup.append(kv)
    dedup.reverse()
    return '; '.join(dedup)
```

3) 在 [python.def main() 内创建 CookieManager 后追加刷新任务](Start.py:1200)（放置在 `cm.manager = cm.CookieManager(loop)` 及初始账号加载完成之后、启动 API 线程之前或之后均可）：
```python
async def _cookiecloud_refresh_loop(manager: cm.CookieManager):
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
        # 默认选择策略：default → 任意已有账号 → 'cookiecloud'
        if 'default' in manager.cookies:
            return 'default'
        if manager.cookies:
            return next(iter(manager.cookies.keys()))
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
                                # 若非 JSON 则直接当字符串拼接
                                parsed = {'cookie_data': {}, 'raw': plaintext}

                            cookie_str = cookiecloud_cookie_string_from_payload(parsed)
                            tcid = await _pick_target_cookie_id()
                            if tcid in manager.cookies:
                                manager.update_cookie(tcid, cookie_str)
                                logger.info(f"CookieCloud 已更新账号 {tcid} 的 Cookie（长度={len(cookie_str)}）")
                            else:
                                manager.add_cookie(tcid, cookie_str)
                                logger.info(f"CookieCloud 已新增账号 {tcid} 并写入 Cookie（长度={len(cookie_str)}）")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"CookieCloud 刷新异常: {e}")
            finally:
                await asyncio.sleep(interval)

# 在 main() 中，CookieManager 创建及本地 Cookie 加载之后追加：
loop.create_task(_cookiecloud_refresh_loop(manager))
```

行为说明
- 未配置 COOKIE_CLOUD_HOST/UUID/PASSWORD：不启动刷新循环，沿用原有 Cookie；
- 已配置：周期性访问 `${HOST}/get/${uuid}`，解析返回 JSON 中的 encrypted 字段：
  - 使用 passphrase = MD5(uuid + '-' + password).substring(0,16) 执行 “Salted__ + EVP_BytesToKey + AES-256-CBC + PKCS7” 解密；
  - 将解密得到的 `{ cookie_data, local_storage_data }` 合并为标准 Cookie 头字符串；
  - 覆盖/创建 `COOKIE_CLOUD_TARGET_COOKIE_ID` 指定的账号（缺省选择 default 或第一个已有账号，否则新建 cookiecloud）；
  - 后续各模块按现有逻辑读写数据库与运行任务（`manager.update_cookie()` 内部已负责重启该账号任务并持久化）。

与第 1~5 节的 API 兼容性
- 本节仅为运行期的“拉取并覆盖账号 Cookie”的补充机制；
- 之前新增的 `/update` 与 `/get/:uuid`（自有 AES-ECB + md5(uuid+password) 前 16 字节的算法）不受影响，可并行存在；
- 两套算法彼此独立：CookieCloud 拉取/解密（Salted__ + EVP_BytesToKey + CBC）仅用于从远端同步，而非本地存储上传。

小提示
- 如需只抽取特定域名的 cookie，可在 `cookiecloud_cookie_string_from_payload()` 中依据 domain 做过滤（例如仅保留包含 goofish/taobao/alibaba/dingtalk 的域），以减少冗余；
- 若你希望新建账号时强制使用固定 ID，可显式设置 COOKIE_CLOUD_TARGET_COOKIE_ID。
