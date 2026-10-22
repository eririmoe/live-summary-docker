# Live Summary Docker

自部署的 Bilibili 录播后处理框架：下播检测 → 等待重连 → FLV 分片无损合并 → 千问长音频转写 → 结合 XML 弹幕生成 DeepSeek 总结 → 网站更新 → 百度网盘备份核验。支持私有 R2 转写归档、认证后台和有证据保护的媒体清理。

主机只需 **Linux、Docker Engine 和 POSIX shell**；Python、FFmpeg、BaiduPCS-Go 均在镜像里。没有 Compose 插件时，`sh compose.sh` 会用临时 Docker CLI 容器执行 Compose。Linux amd64 为验证目标；镜像下载器也提供 arm64 固定校验值，arm64 仍需自行验收。建议至少 2 核、2 GB 内存，磁盘容纳原片、合并录像和临时音轨；需要出站访问镜像源、GitHub、直播平台及所选 API。

**默认不自动处理、不删除文件、不向 Cloudflare 发布。** 每个部署处理一个直播间；多个直播间请使用独立目录、Compose project 名称和端口。ASR 与总结调用会计费。`enable` 会处理指定目录里所有未认领的稳定 FLV，包括旧录像，请先核对目录。

## 1. 获取代码：不要求安装 Git 或 Python

在目标机器新建专用目录，通过 Docker 内的 Python 下载公开源码；下载内容是代码，不含配置或密钥。生产部署建议将 `REF` 改为已经审阅的提交 SHA。

```sh
mkdir live-summary-docker
cd live-summary-docker
docker run --rm -i --user "$(id -u):$(id -g)" -v "$PWD:/out" python:3.12-slim-bookworm python - <<'PY'
import io, pathlib, urllib.request, zipfile
REF = 'main'
url = 'https://github.com/eririmoe/live-summary-docker/archive/' + REF + '.zip'
data = urllib.request.urlopen(url, timeout=120).read()
with zipfile.ZipFile(io.BytesIO(data)) as archive:
    for entry in archive.infolist():
        parts = pathlib.PurePosixPath(entry.filename).parts[1:]
        if not parts or entry.is_dir(): continue
        if '..' in parts: raise SystemExit('Unsafe archive member')
        target = pathlib.Path('/out').joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(entry))
PY
```

也可以 `git clone https://github.com/eririmoe/live-summary-docker.git`。命令均在项目根目录执行。不把已有业务目录或 HOME 当作项目数据目录。

## 2. 初始化私有配置

```sh
docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD:/workspace" -w /workspace \
  python:3.12-slim-bookworm python scripts/setup.py
sh compose.sh build
```

向导要求真实长房间 ID、主播显示名、录播子目录和管理凭证，API Key 隐藏输入，可暂时跳过。新机器使用默认 `http://bilirecorder:2356`；`record_dir` 由脚本配置给新建房间。已有录播请阅读[接入指南](docs/EXISTING-RECORDER.md)。重复运行初始化会拒绝覆盖已有配置。

生成的 `.env`、`data/config.json`、`data/secrets.json` 和 `data/recorder-secrets.json` 均不进入 Git，Linux 文件权限为 0600，数据目录为 0700。随机生成的管理密码保存在这些私有文件内，请通过自己的安全密码管理方式保存，勿粘贴到 agent 对话或日志。应用无 Docker socket，worker 的录播挂载只读，admin 的状态挂载只读。

给自动化 agent 的无交互配置格式见 [deployment-input.example.json](deployment-input.example.json)。将已安全填充的内容保存为 `deploy.private.json` 后使用：

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/workspace" -w /workspace \
  python:3.12-slim-bookworm python scripts/setup.py --input /workspace/deploy.private.json
```

实际密钥文件由部署者提供；agent 不应把密钥拼接进命令、提交或报告。向导将当前 UID/GID 写入私有 `.env`，应用与新录播容器使用相同用户读写专用目录，避免 0700/0600 文件的跨用户权限问题。绝对数据路径需要在初始化容器里按**相同绝对路径**额外挂载；最简单且可直接运行的默认布局是项目下的 `data/` 和 `records/`。详细执行契约见 [AGENTS.md](AGENTS.md)。

## 3. 从零启动录播和服务

```sh
sh compose.sh --profile recorder up -d recorder worker admin
sh compose.sh run --rm tools python scripts/manage.py register-room
sh compose.sh run --rm tools python scripts/manage.py wire-recorder
```

`register-room` 新建房间时先关闭自动录制，设置确定的目录及默认时间文件名；已有房间的设置不覆盖。`wire-recorder` 增补 Webhook V2，保留其他 URL，保存私有备份并回读校验，重复执行不重复添加。扫描器还会补发现遗漏事件。Webhook 地址只在 Docker 内部使用，不要公开或记录 token。

本地入口：公开总结 [localhost:8765](http://localhost:8765)、认证后台 [localhost:8766](http://localhost:8766)、录播管理 [localhost:2356](http://localhost:2356)。后台使用初始化的录播管理账号。远程机器可用 SSH 端口转发；后台本地 HTTP 的 cookie 模式仅允许 loopback origin。对外发布管理后台前必须配置 HTTPS origin 和 Secure cookie，见[运维说明](docs/OPERATIONS.md)。

## 4. 百度登录与供应商配置

```sh
# BaiduPCS-Go 的交互式 shell；在其提示符下执行 login，按当前版本提示登录，完成后 quit
sh compose.sh run --rm tools BaiduPCS-Go

# 补填/更新千问、DeepSeek、可选 Cloudflare API Key，输入隐藏
sh compose.sh run --rm tools python scripts/manage.py credentials

# 查看配置和录播 API 状态；缺少录播目录或任何必要配置时返回非零
sh compose.sh run --rm tools python scripts/manage.py doctor
```

百度会话只保存在 `data/baidu/`。远端目标为 `baidu_root/record_dir/`，首次部署请确认目录已存在且账号有权限。CLI 退出码 0 不证明备份成功；任务完成前还会逐文件核对远端精确字节数。

千问采用 [Qwen-Audio Filetrans HTTP API](https://platform.qianwenai.com/docs/api-reference/speech-recognition/fun-asr-recording/restful-api)，默认模型 `qwen-audio-3.1-asr-flash-filetrans`，基地址 `https://maas.qianwenaiapi.com/api/v1`，需支持文件上传的通用按量 Key。当前实现使用千问临时上传，不是通用 OpenAI ASR 接口，也没有实现任意 OSS 后端。DeepSeek 默认 `deepseek-flash`、`https://api.deepseek.com`；部署者应确认自己账号可用的模型和配额。

默认音轨每块一小时、单声道 16 kHz MP3。异步任务保存 task ID；重启后轮询原任务。提交结果不确定时停止自动重提，避免重复计费。房间词表默认关闭，`asr-vocabulary.json` 只有合成示例，应换成自己的已核验词表后再开启。完整转写、原始模型响应及检查点保存在私有状态目录，公开站仅含总结及条目元数据。

## 5. 验收后开启全流程

先完成下文离线测试；再显式开启录制，获得一段合法且完整的测试录播：

```sh
# 允许录播姬在该房间开播时自动录制；可能立即开始录制
sh compose.sh run --rm tools python scripts/manage.py record

# 找到已关闭文件后，执行 60 秒付费样本；不整场备份、不公开样本
sh compose.sh exec worker python app.py smoke
sh compose.sh run --rm tools python scripts/manage.py doctor

# 样本和目录范围验收通过后才开启整场处理
sh compose.sh exec worker python app.py enable
sh compose.sh exec worker python app.py status
```

`doctor` 不替代真实录制或付费 API 验收；空录播目录时它会失败，首次录制前这是预期结果。生产自动化应先拿到部署者对真实录制及计费测试的授权。等待录播停止且连续离线十分钟、文件稳定后，worker 才入队。新直播优先于历史任务，历史任务在安全阶段边界让位，已付费请求不会被主动中断。

网站默认由 worker 提供本机静态文件。改用 Cloudflare Pages、配置 R2、手动清理和历史回填分别见 [配置说明](docs/CONFIGURATION.md) 与[运维说明](docs/OPERATIONS.md)。不会自动创建付费账户、购买域名或修改既有 DNS。

## 测试与验收证据

```sh
# 隔离测试不挂载任何部署数据或录播，无网络，不调用付费 API
docker run --rm --read-only --network none --tmpfs /tmp:rw,size=256m \
  -e LIVE_SUMMARY_STATE=/tmp/test-state -e LIVE_SUMMARY_RECORDS=/tmp/test-records \
  live-summary:local python -m unittest discover -s tests -v

# 有 Node 时可运行浏览器搜索逻辑测试；也可使用临时容器
docker run --rm -v "$PWD:/app:ro" -w /app node:22-bookworm-slim node tests/test_archive_search.cjs

# 不需 Git 的源码扫描；仓库维护者推送前使用 --tracked
docker run --rm -v "$PWD:/workspace:ro" -w /workspace \
  python:3.12-slim-bookworm python scripts/scan_public.py
```

CI 构建实际镜像，并运行原有处理链回归、真实 FFmpeg 分片测试和新部署测试。测试媒体、API 响应和凭证均为合成夹具。当前交付的实际验证范围与限制见 [VERIFICATION.md](docs/VERIFICATION.md)。检查通过不代表每个供应商的新账号都已通过真实计费调用。

## 项目结构与恢复边界

| 路径 | 用途 |
| --- | --- |
| `compose.yaml`、`Dockerfile` | 容器、资源边界与固定依赖 |
| `scripts/setup.py`、`scripts/manage.py` | 初始化、隐藏输入、录播接线、诊断 |
| `core.py`、`app.py`、`scheduler.py` | SQLite 队列、事件、扫描与优先调度 |
| `pipeline.py`、`merging.py`、`providers.py` | 媒体处理、转写、总结、发布、备份 |
| `history.py`、`audio_overlap.py` | 有界历史回填、重叠检查、空壳 FLV 诊断 |
| `storage.py`、`admin.py` | 私有归档和认证后台 |
| `site/`、`admin-site/` | 通用前端，无个人头像或私有素材 |
| `data/`、`records/` | 仅部署时生成，严禁提交或复制到公开镜像 |

升级前暂停入队并等待 running=0，备份整个私有状态（包含 SQLite WAL 和付费 task ID），再构建/替换镜像。完整流程见运维说明。只回滚代码、不恢复检查点可能导致重复请求；不要删除 `data/jobs/` 来解决失败任务。

项目代码采用 [MIT License](LICENSE)。BaiduPCS-Go、FFmpeg、Python、录播姬等组件保留各自许可证，见 [THIRD_PARTY.md](THIRD_PARTY.md)。录播、弹幕和转写的使用及发布范围由部署者确认。
