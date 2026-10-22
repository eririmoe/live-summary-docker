# 运维与恢复

## 运行检查

```sh
sh compose.sh ps
sh compose.sh exec worker python app.py status
sh compose.sh logs --tail 100 worker
sh compose.sh exec worker python app.py pause
```

`pause` 停止新任务入队和开始处理，不中断已经执行的付费请求。更新前等待 `status` 中 running=0。查看日志时不要发送私有文件、原始模型响应、完整环境变量或签名 URL。容器日志轮转默认每文件 10 MB、最多三份。

进程重启会把 running 恢复到 queued，再复用对应检查点。看到 `submission_uncertain` 且没有 task ID 时，先去供应商侧核实，不删除检查点强行重提。五次失败的历史任务保留 failed 及错误，不伪装为完成；新直播仍优先。

## 更新、备份和回滚

1. `app.py pause`，等待 running=0；停止 worker/admin，避免不一致的 SQLite/WAL 文件复制。
2. 在私有存储备份 `.env`、整个 `data/`、已有 recorder 的配置，和按保留计划需要的 recordings。备份含密码、cookie 和原始转写，必须加密并控制访问。不要只复制 `state.sqlite3` 而遗漏 WAL。
3. 保存旧镜像 ID/标签及当前源提交，切换到已审阅新版本，构建镜像。
4. `sh compose.sh up -d worker admin`，保持暂停，运行 doctor，检查旧 task ID/结果仍在。
5. 验收通过再 enable；回滚时换回旧镜像和兼容的完整状态快照，避免丢失更新后产生的付费任务记录。旧检查点不应因代码升级被全量重做。

`down` 不会删除 bind-mounted data，但会拆本项目网络；接入了外部 recorder 时先按接入文档断开。不要使用 `down -v` 或 `docker system prune` 作为日常清理命令。

## HTTPS 后台和外网访问

默认端口全部绑定 127.0.0.1。远程操作可建立：

```sh
ssh -L 8765:127.0.0.1:8765 -L 8766:127.0.0.1:8766 -L 2356:127.0.0.1:2356 YOUR_HOST
```

浏览器访问 `http://localhost:8766`。后台的 local HTTP cookie 只用于回环地址。使用自己已有的 HTTPS reverse proxy 或 Cloudflare Tunnel 时，把 `status.example.com` 映射至主机 `http://127.0.0.1:8766`；将 `.env` 的 `ADMIN_ORIGIN` 改为精确 `https://status.example.com`，`ADMIN_COOKIE_SECURE=true`，只重建 admin：

```sh
sh compose.sh up -d --no-deps admin
```

容器内 Tunnel 的 localhost 指向自身；这种模式需将其加入后处理 Docker 网络，并转发到 `http://admin:8081`。本项目不替部署者创建 Tunnel/DNS，不将管理 API 或 webhook token 发布到网站。会话只在内存保存，重启失效；认证后台 no-store，登录限流按直连来源地址统计，代理后可能由多个用户共享额度。公开站可独立放 Pages，后台仍须认证。

## 两种手动媒体维护方式

两种方式均默认不启用。只使用其中一种策略，先审阅预览，再单独授权删除。maintenance 是一次性容器，拥有录播目录写权限；正常应用始终只读。

**保留完整合并录播、删除原分片**：

```sh
# 预览，要求已完成任务、合并哈希、网盘核验与发布回执
sh compose.sh run --rm maintenance python tools/23-consolidate-recordings.py
# 审核候选并获得授权后：
sh compose.sh run --rm maintenance python tools/23-consolidate-recordings.py --apply
```

这一步会把经验证的合并 FLV/XML 安装回录播目录，然后仅移除匹配指纹的源 FLV 分片，保留源 XML 和证据。它不等同于删除全部完成视频，不要求设置 delete_completed_media。源文件变化或录像仍在线则拒绝操作。合并前后检查为总时长及开头/末尾/接缝短时解码，不能证明每帧中间内容均完好。

**云端证据完成后删除本地视频及提取音轨**：须先配置私有 R2，任务 done，R2 转写完整校验、发布和网盘备份回执齐全，并重新确认远端视频大小。预览：

```sh
sh compose.sh run --rm maintenance python tools/24-clean-completed-media.py
```

获得明确授权后，私有 `data/config.json` 设置 `delete_completed_media=true`，再执行 `--apply`。仅删除计划中指纹/哈希仍匹配的 FLV 和 audio-NNN.mp3，XML、TXT、JSON、转写、总结和付费检查点保留。直播中、证据缺失或源改变会停止；所有对象的删除回执会保存。本项目不自动安装宿主机 systemd/timer；需要无人值守时，应自行审阅并接入这一显式命令，不能直接对整个录播目录递归删除。

`cleanup.py` 保留旧的“按天数、最近数量”预览/策略接口，默认 cleanup_apply=false；推荐使用上述证据更完整的维护入口，避免混用策略。

## 有界历史回填（高级）

`history.py` 保留一次初始十场、后续每次二十场的有限队列逻辑，新直播优先。它不是自动遍历并处理整个网盘。启用前需要：私有 R2、精确网盘 inventory、日期上界、容量预算与计费授权。`data/runtime/baidu-history-inventory.json` 格式：

```json
{"files":[
  {"name":"录制-123456789-20260101-120000-000-示例.flv","path":"/live-recordings/123456789-recordings/录制-123456789-20260101-120000-000-示例.flv"},
  {"name":"录制-123456789-20260101-120000-000-示例.xml","path":"/live-recordings/123456789-recordings/录制-123456789-20260101-120000-000-示例.xml"}
]}
```

需足够十场，示例仅说明格式。先使用 `history.plan(inventory)` 在专用工具脚本里预览、核对去重和场次边界，再执行 `app.py history-start --limit 10`。它会读取远端 metadata，核对同名别名的大小和可靠哈希；XML 缺失或冲突会停止。初始批次与后续扩展有幂等记录；不要删除 batch 文件重建来绕过去重。历史路径始终限制在配置 baidu_root 内，房间和日期不使用旧部署常量。

历史处理每次只下载一个 FLV；转写/R2 完成后可释放可重下载缓存。只有严格解析证明完全没有音视频标签的 metadata-only FLV 才会跳过 ASR，不按文件小或 ffprobe 一般失败跳过。超长断流缺口需要与源哈希绑定的人工边界复核，不通过改时间或伪造回执绕过。
