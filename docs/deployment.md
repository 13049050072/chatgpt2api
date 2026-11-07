# 部署与升级

状态：当前

本项目的发布镜像默认是 `ghcr.io/yukkcat/chatgpt2api:latest`。标准 Compose 将服务暴露在 `3000` 端口，使用 `chatgpt2api-runtime` 命名卷保存可更新的应用运行目录，并单独挂载本地 `data/` 和 `config.json`。运行时配置和数据不应提交到 Git。

## Docker 部署

```bash
git clone https://github.com/yukkcat/chatgpt2api.git
cd chatgpt2api
cp .env.example .env
# 将 .env 中的 CHATGPT2API_AUTH_KEY=your_secret_key_here 替换为私有密钥。
test -f config.json || printf '{}\n' > config.json
docker compose up -d
```

镜像中的 `/opt/chatgpt2api` 是只读应用种子，`/app` 是受管运行目录。首次启动或镜像版本变化时，入口脚本会用镜像种子刷新 `/app`，再按锁文件同步 Python 依赖；同一镜像正常重启时会保留控制台在线更新后的运行版本。业务数据始终留在独立的 `data/` 挂载中。

### 本地 PostgreSQL 18

需要由 Compose 一并运行 PostgreSQL 时，在 `.env` 中设置数据库密码：

```dotenv
POSTGRES_PASSWORD=replace_with_a_strong_password
```

然后同时加载基础 Compose 和 PostgreSQL overlay：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

`docker-compose.postgres.yml` 使用官方 `postgres:18-alpine` 镜像，等待数据库健康后再启动应用，并将数据持久化到 `chatgpt2api-postgres-data` 命名卷。数据库端口默认不暴露到宿主机。

`POSTGRES_PASSWORD` 会同时用于初始化数据库和构造 `DATABASE_URL`，因此请仅使用 URL 安全字符（字母、数字、下划线或连字符），不要在两个位置分别编码密码。
启用该模式后，后续启动、升级、查看状态和停止服务都应同时指定这两个 Compose 文件。

查看状态与日志：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml ps
docker compose -f docker-compose.yml -f docker-compose.postgres.yml logs -f postgres app
```

`.env` 中的 `CHATGPT2API_AUTH_KEY` 优先于 `config.json` 的 `auth-key`。若要使用 `config.json`，先删除或注释 `.env` 中的该值，再填写 `auth-key`。

默认地址：

- 控制台：`http://localhost:3000`
- API：`http://localhost:3000/v1`

也可以使用仓库安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/yukkcat/chatgpt2api/main/deploy/install.sh | sudo bash
```

安装脚本会明确询问 Application Database：

- SQLite 本地文件（默认）
- PostgreSQL 18 本地容器（Docker 模式）
- 已有 PostgreSQL URL

选择本地 PostgreSQL 时，脚本会自动生成并保存数据库密码，下载 `docker-compose.postgres.yml`，再与主 Compose 一起启动。重复运行安装脚本会复用已有密码。`CHATGPT2API_THREAD_TOKENS` 默认是 `120`，表示后端同步工作线程的并发容量，只要求正整数且不设置人为最高值；账号、代理和上游服务仍分别执行自己的并发限制。

### 密钥错误锁定

同一来源 IP 在 5 分钟内用错误密钥请求 10 次后，会被锁定 15 分钟。锁定期间，这个 IP 带密钥的请求一律返回 429 和 `Retry-After`，正确的密钥也不例外；不带密钥的请求（例如打开控制台页面）不受影响。计数只保存在进程内存中，服务重启后清零。可以在 `.env` 中调整：

- `CHATGPT2API_AUTH_FAILURE_LIMIT`：触发锁定的错误次数，默认 `10`，设为 `0` 关闭锁定。
- `CHATGPT2API_AUTH_FAILURE_WINDOW_SECONDS`：统计错误次数的时间窗口，默认 `300` 秒。
- `CHATGPT2API_AUTH_LOCKOUT_SECONDS`：锁定时长，默认 `900` 秒，设为 `0` 关闭锁定。

服务部署在 Nginx、Caddy 等反向代理之后时，必须设置 `FORWARDED_ALLOW_IPS`。否则服务看到的来源地址都是代理地址，一个人反复输错密钥会把所有人一起锁定。把它设为代理连到服务时使用的地址，多个地址用逗号分隔；宿主机上的代理转发到 Docker 容器时，这个地址通常是 Docker 网络的网关地址（例如 `172.18.0.1`，以容器看到的来源地址为准）。只有服务端口不对外开放时才能设为 `*`，否则客户端可以伪造 `X-Forwarded-For` 绕过锁定。源码运行时，在启动 uvicorn 的环境中设置同名环境变量。

## 本地开发

后端：

```bash
git clone https://github.com/yukkcat/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

Vue 控制台：

```bash
cd web-vue
npm install
npm run dev
```

前端开发服务器默认使用 Vite 端口；后端仍读取项目根目录的 `config.json` 和 `data/`。

## 存储边界

`DATABASE_URL` 选择 Application Database；未设置时使用
`data/chatgpt2api.db`。支持 SQLite 与 PostgreSQL 18，不再通过
`STORAGE_BACKEND` 选择 JSON、Git 或账号专用数据库。

选择数据库不是旧数据迁移操作，不会自动导入 JSON、JSONL、Git 或旧账号
SQLite 文件。图片文件及其相关索引仍按图片存储边界管理。完整边界见
[`storage-architecture.md`](storage-architecture.md)。

## 升级

升级前先在系统设置中执行一次 R2 备份，并确认状态为成功。备份归档始终包含
Application Database：SQLite 使用 `data/application-database.sqlite3`，PostgreSQL
使用 `data/application-database.pgdump`。图片任务记录、PPT / PSD 文件和图片目录
按备份设置选择；外部 WebDAV 仍需独立备份。

备份归档包含全部账号凭据，建议在备份设置中启用加密。加密备份以 `.tar.gz.enc`
结尾，使用 OpenSSL AES-256-CBC 与 PBKDF2-SHA256（600000 次迭代）；对象元数据
`x-amz-meta-kdf` 记录迭代次数，`x-amz-meta-ciphertext-hmac-sha256` 记录密文的
HMAC-SHA256。恢复前先计算 HMAC，与元数据一致后再解密：

```bash
export CHATGPT2API_BACKUP_PASSPHRASE='<加密口令>'
python3 - backup-xxx.tar.gz.enc <<'PY'
import hashlib, hmac, os, sys
with open(sys.argv[1], "rb") as source:
    header = source.read(16)
    key = hashlib.pbkdf2_hmac("sha256", os.environ["CHATGPT2API_BACKUP_PASSPHRASE"].encode(),
                              b"chatgpt2api-backup-hmac-v1" + header[8:], 600000)
    mac = hmac.new(key, header, hashlib.sha256)
    for chunk in iter(lambda: source.read(1 << 20), b""):
        mac.update(chunk)
print(mac.hexdigest())
PY
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 \
  -in backup-xxx.tar.gz.enc -out backup.tar.gz -pass env:CHATGPT2API_BACKUP_PASSPHRASE
```

没有 `x-amz-meta-kdf` 元数据的旧加密备份不含 HMAC，解密时去掉 `-iter 600000`。

也可以在系统设置的备份历史中点击加密备份的“校验”：服务用当前保存的加密口令流式
读取 R2 上的对象并重新计算 HMAC，与元数据比对后返回结果，不落盘也不解密。下载到
本地的文件仍应按上面的命令校验后再恢复。

未配置 R2 时，应先停止服务再备份。SQLite 可以在停服后复制 `data/chatgpt2api.db`；
PostgreSQL 必须使用 `pg_dump --format=custom`，不能用 `tar data/` 代替数据库备份。
`config.json` 只保留 `auth-key` 等启动配置，也应单独保存：

```bash
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

本地 PostgreSQL Compose 可直接在数据库容器内导出：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T postgres \
  sh -c 'pg_dump --format=custom --no-owner --no-privileges \
  -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

### 控制台在线更新

使用当前标准 Compose 部署时，版本弹窗可直接启动在线更新。后端负责下载发布包、校验 SHA-256 与更新清单、替换运行文件、同步依赖和安排容器重启；前端只展示后端任务，刷新页面或重启完成后仍可恢复任务结果。文件或依赖同步失败时会恢复旧文件，并按旧锁文件重新同步依赖。

源码运行和没有挂载受管 `/app` 运行目录的旧容器不会显示“立即更新”，只会给出 Git 或镜像升级提示。首次切换到受管运行目录时，应先更新仓库中的 Compose 文件，再执行一次镜像升级：

```bash
git pull --ff-only
docker compose pull
docker compose up -d
```

不要把 `/app` 运行时卷当作业务备份；它可以从发布镜像重新创建。不要使用 `docker compose down -v` 执行普通升级，因为该命令还会删除 Compose 管理的命名卷。

### 命令行升级

镜像部署升级：

```bash
docker compose pull
docker compose up -d
```

本地 PostgreSQL Compose 部署升级：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml pull
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

镜像部署固定或回退版本时，在 `.env` 设置 `CHATGPT2API_IMAGE=ghcr.io/yukkcat/chatgpt2api:<tag>`，再执行对应的 `pull` 与 `up`。镜像版本变化后，入口脚本会用该镜像刷新受管运行目录；Git 检出标签只影响源码运行，不会改变 Compose 使用的镜像版本。升级后检查：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 回滚与维护

先停止对应 Compose，再恢复经过验证的代码 / 镜像和备份数据；不要在运行时直接覆盖 `data/`。常用命令：

```bash
docker compose restart
docker compose down
```

`docker image prune` 只清理未使用镜像，不会替代数据备份。
