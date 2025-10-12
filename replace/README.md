## 环境变量

- COOKIE_CLOUD_HOST: CookieCloud 服务器地址，例如 https://cookiecloud.25wz.cn/ 或 http://45.138.70.177:8088 （若公共服务器无法使用，请参考 https://github.com/easychen/CookieCloud/blob/master/README_cn.md 并自建服务端）
- COOKIE_CLOUD_UUID: 在 CookieCloud 中配置的 uuid
- COOKIE_CLOUD_PASSWORD: 用于在服务端解密返回明文 cookie_data 的密码
- COOKIE_CLOUD_REFRESH_SECONDS: 刷新间隔（秒），默认 1800。也支持变量 COOKIE_CLOUD_REFRESH_INTERVAL
- COOKIE_CLOUD_COOKIE_ID: 可选，目标账号 ID；不填时优先 default，其次单账号时使用该账号

行为约定：
- 未配置 COOKIE_CLOUD_HOST 或 COOKIE_CLOUD_UUID 时，保持原有 Cookie，不改动
- 配置完整后：启动前先拉取一次并覆盖数据库与内存；随后后台定时按间隔同步，并热更新正在运行的账号任务

## 替换/新增的文件

- Start.py: 启动入口，新增 CookieCloud 首次拉取与后台定时刷新逻辑
- utils/cookiecloud.py: 从 CookieCloud 拉取并将 cookie_data 合并为标准 Cookie 字符串
- replace/filelist.txt: 列出需替换的目标相对路径（用于脚本自动拉取）

## 一键替换（推荐），
### 在 xianyu-auto-reply 的根目录执行下面代码，然后再构建 Dockers

Linux/macOS（或 Windows 的 Git Bash/WSL）：

```bash
curl -fsSL https://raw.githubusercontent.com/OnlineMo/Goofish-Auto-reply-replace/refs/heads/main/replace.sh | bash
```

可选：自定义仓库分支/地址

```bash
REPLACE_BASE_URL=https://raw.githubusercontent.com/OnlineMo/Goofish-Auto-reply-replace/refs/heads/main bash -c "$(curl -fsSL https://raw.githubusercontent.com/OnlineMo/Goofish-Auto-reply-replace/refs/heads/main/replace.sh)"
```

## 备选：Python 脚本替换（跨平台）

```bash
python3 apply_replace_from_github.py
```

可选参数：

```bash
python3 apply_replace_from_github.py --base https://raw.githubusercontent.com/OnlineMo/Goofish-Auto-reply-replace/refs/heads/main
python3 apply_replace_from_github.py --dry-run
```

替换脚本行为：
- 远程读取 replace/filelist.txt
- 依次下载 replace/{path} 并覆盖本地 {path}
- 覆盖前将旧文件备份至 backup_replace_YYYYmmdd_HHMMSS/

## 运行效果验证

- 首次启动日志应出现："CookieCloud 首次同步完成" 字样
- 后台每隔 N 秒（默认 1800）打印刷新结果，失败不影响现有 Cookie
- 账号任务已在运行时将触发热更新（CookieManager.update_cookie），重启对应账号的主循环

## 回滚说明

- 所有被替换文件都会在项目根目录备份到 backup_replace_YYYYmmdd_HHMMSS/
- 如需回滚，直接用备份目录中的同名文件覆盖当前文件

## 文件清单（replace/filelist.txt）

```text
Start.py
utils/cookiecloud.py
```

## 变更摘要

- Start.py: 新增 _setup_cookiecloud_before_start 与 _cookiecloud_refresh_loop 流程，读取环境变量并在启动前覆盖 + 后台刷新
- utils/cookiecloud.py: 实现 fetch_cookiecloud_cookie_str(host, uuid, password, timeout=15)，优先Post请求 /get/:uuid 返回明文，解析 cookie_data 合并为标准 Cookie 字符串

## 注意事项

- 若服务端未开启 password 解密，/get/:uuid 返回 encrypted 字段，脚本将记录警告并跳过更新
- 若项目在 Windows 环境且没有 Bash，可直接使用 Python 脚本进行替换
- 刷新间隔设置过短可能导致服务端限流，建议 ≥ 300 秒（最小60s，小于60s则自动设为60s）

完成后，设置环境变量并正常启动项目即可生效。
