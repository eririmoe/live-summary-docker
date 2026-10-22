# 配置参考

主配置是 `data/config.json`，覆盖只读 `config.example.json`。运行配置不会自动进入镜像。一个部署只对应一个 canonical room ID。

| 项 | 作用与约束 |
| --- | --- |
| `room_id`、`streamer` | 真实长房间 ID 与显示名；网页显示名会 HTML 转义 |
| `record_dir` | `/records` 下一个子目录，不接受绝对路径、`..` 或符号链接源文件 |
| `recorder_url` | Docker 网络内的录播 API，无内嵌凭证；默认 `http://bilirecorder:2356` |
| `recording_timezone` | 文件名时间戳的 UTC offset；Bilibili 默认 `+08:00` |
| `baidu_root` | 明确的网盘备份根目录；默认 `/live-recordings`，不是本地路径 |
| `asr_model`、`asr_base` | 千问 Filetrans 模型及兼容基地址 |
| `audio_chunk_seconds` | 默认 3600；单声道 16 kHz、48 kbps MP3 |
| `summary_model`、`summary_base` | 默认 DeepSeek 官方接口；保留 JSON、类型与时间范围验证 |
| `stable_seconds` | 文件最短稳定时间，默认 600 秒 |
| `reconnect_grace_seconds` | 连续离线重连等待，默认 600 秒 |
| `merge_gap_seconds` | 归组边界，默认 900 秒 |
| `scan_interval` | 独立发现线程扫描间隔，默认 60 秒 |
| `publish_backend` | `local` 或 `cloudflare-pages` |
| `transcript_archive` | 默认 `{"backend":"local"}`；也可私有 R2 |
| `auto_process` | 默认 false；通过 app.py enable/pause 变更 |
| `cleanup_apply`、`delete_completed_media` | 默认 false；两个不同清理策略均不自动启用 |
| `history_before` | 历史回填日期上界 YYYYMMDD，不包含该日；应先预览，不能推测原业务日期 |
| `asr_context_enabled` | 默认 false；只影响新提交 ASR，已有 task ID 和结果继续复用 |

初始化只写配置，不连接 API，不开启录制或付费请求。绝对 `DATA_DIR`、`RECORDS_DIR` 必须在 bootstrap 容器内挂载到与主机相同路径，例如：

```sh
docker run --rm -it -v "$PWD:/workspace" -v /srv/live-summary:/srv/live-summary \
  -v /srv/records:/srv/records -w /workspace \
  python:3.12-slim-bookworm python scripts/setup.py
```

非交互输入的 `deployment.data_dir`、`deployment.records_dir` 分别填上述绝对路径；交互向导的状态默认仍在 `./data`。不要把共享业务目录权限改成 0700；应给服务分配独立目录。

构建默认使用 Debian 官方 HTTPS 软件源。如果网络访问缓慢，可显式选择自己信任的镜像，例如 `sh compose.sh build --build-arg DEBIAN_MIRROR=https://mirrors.aliyun.com`。该参数只影响 Debian 软件包；BaiduPCS-Go 仍从官方 GitHub Release 下载并校验固定 SHA-256。不要关闭 TLS 或跳过哈希核验来绕过下载失败。

## Cloudflare Pages

先保持暂停。设置 `publish_backend=cloudflare-pages`、一个全新且唯一的 `pages_project`，并配置 `cloudflare_account_id`。Token 需目标账户 Pages Edit。仅使用默认 pages.dev 地址时 `site_domain`、`zone_name` 可以留空，不需要 DNS 权限。供应商账号及网络差异会影响 pages.dev 可达性。

```sh
sh compose.sh run --rm tools python scripts/manage.py credentials
sh compose.sh exec worker python app.py deploy
```

第一次部署创建 Pages 项目并保存 `pages-project.json` 所有权记录。若项目已存在却没有本地记录，脚本停止，不接管未知项目。部署后回读 `data/archive.json` 与本地精确比较，才写 `last-deployment.json`。不要手工伪造 ownership 或 published 回执。

自定义域名另需 `site_domain`、`zone_name`、目标 Zone Read/DNS Edit 权限，明确授权后执行：

```sh
sh compose.sh exec worker python app.py bind-domain
```

遇到同名且不同目标 DNS 会停止，不覆盖。证书和域名状态可能尚未激活，需继续核查，不以 API 接受请求代替完成。

## 私有 R2 转写归档

创建自己账户下的私有 bucket，保持 Public Access 关闭，创建仅该 bucket 的 Object Read & Write S3 凭证，然后运行：

```sh
sh compose.sh run --rm tools python scripts/manage.py configure-r2
```

不要求先有 Pages 项目。向导写 `r2-secrets.json` 和 `transcript_archive` 配置，默认 prefix 为 `transcripts`。对象按 `<prefix>/<room_id>/YYYY/MM/DD/<job_id>/` 保存 JSON、TXT 和 manifest；逐个完整回读，比较字节数和 SHA-256 后记录回执。

配置 R2 不证明 bucket 真的私有，也不证明上传成功；部署者须在控制台核对访问策略，在首个完整任务核对归档回执。后台通过已认证请求签发短时下载链接。链接是临时凭证，不写日志、不贴聊天。local 模式在认证后直接提供本机转写，不依赖 R2。

## 词表和时间

默认词表仅为合成示例，无历史观众语料。替换 `asr-vocabulary.json` 后重新构建镜像，再启用 `asr_context_enabled`。新提交前验证词条重复、权重和上下文长度；超限直接失败，不静默截断。不要把观众推测作为主播事实，也不要对旧转写全局同音替换。

新建录播房间的命名模板固定采用 Asia/Shanghai。`recording_timezone` 主要用于接入已有自定义命名配置；两者必须一致。系统不能从含糊的文件名自动推断时区。
