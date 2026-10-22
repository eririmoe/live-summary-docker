# 接入已有录播姬

不启动本项目的 recorder profile，不停止或重建已有录播容器。

1. 先只读查看已有容器的录播挂载、Docker 网络、管理端口、文件名和 canonical room ID。不要把 `docker inspect` 完整输出粘贴到对话，因为它可能含密码。
2. 初始化时填写已有管理账号，`records_dir` 指向实际主机录播根目录，`record_dir` 精确匹配其房间子目录；默认文件名应类似 `录制-<room_id>-YYYYMMDD-HHMMSS-fff-标题.flv`。后处理挂载内路径统一为 `/records`，不可在已有队列生成后随意更换。

   后处理 UID/GID 必须能读取原录播目录和新生成文件，且能写自己的私有状态。新项目默认沿用 bootstrap 用户的 UID/GID。已有录播若是 root 创建的 0600 文件，应选择匹配的部署用户或经过审核的组权限策略，不能靠关闭文件保护或递归 chmod 777 解决。
3. 将已有容器接入本项目网络，提供 `bilirecorder` 别名。若 Docker 网络里已有这个别名，先核实冲突，不把两个不同录播端点混在一起。

```sh
# 项目首次启动，创建其网络；只启动后处理
sh compose.sh up -d worker admin

# 将 EXISTING_RECORDER 换成部署者授权的容器名；实际网络名由 COMPOSE_PROJECT_NAME 决定
docker network connect --alias bilirecorder live-summary_backend EXISTING_RECORDER

sh compose.sh run --rm tools python scripts/manage.py wire-recorder
sh compose.sh run --rm tools python scripts/manage.py doctor
```

已连接时不重复 connect。`wire-recorder` 保留其他 Webhook URL，但确实会修改目标录播姬配置，部署者需授权这一接入动作。已有房间不调用 `register-room`/`record`，由原录播配置继续控制录制。确认 directory、稳定性、磁盘容量和既有未处理文件范围后再决定是否付费 smoke 与 enable。

如果录播不在 Docker 同一主机，可以用受控内网 HTTPS API 配合共享或同步后的只读录播目录；路径和文件稳定性必须一致。不要把 Docker 端口或管理 API 为了方便直接暴露公网，不使用无人校验的 rsync 半成品作为已关闭录像。

回退：暂停 worker，等待当前任务完成；删除**仅由本项目新增**的 Webhook URL，然后 `docker network disconnect live-summary_backend EXISTING_RECORDER`。保留之前的录播配置和其他 URL。停止 worker/admin 后仍保留 data 与 records，不运行 down -v 或目录清空脚本。
