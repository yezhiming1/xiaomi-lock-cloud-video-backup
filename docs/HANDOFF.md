# 使用与交接

## V0.0.2 目标发现

- 优先使用 Xiaomi Miot 已加载云会话的设备列表，并按内存设备编号合并同一物理设备。
- 设备列表没有目标型号时，才使用 Xiaomi Miot 已加载实体索引；实体云对象必须属于
  已加载会话。
- 无目标返回 `TARGET_MATCH_NONE`，多个不同目标返回 `TARGET_MATCH_MULTIPLE`。
- 固定错误码、服务响应和诊断均不包含设备编号、账号字段或认证值。

## 安装前

1. 确认 Home Assistant 中的 `hass-xiaomi-miot` 已登录且能正常读取设备。
2. 确认目标设备型号；默认实现只对 `xiaomi.lock.s1` 的事件发现路径有实际观察。
3. 在 `/media` 下准备可写父目录。NAS 网络存储必须由用户事先在 Home Assistant
   中配置，本集成不创建共享或挂载。
4. 确认目标文件系统支持同目录硬链接与 `fsync`；否则无覆盖原子发布会返回
   `ATOMIC_PUBLISH_UNAVAILABLE` 或 `OUTPUT_SYNC_FAILED`。
5. 备份 Home Assistant 配置，并记录当前可回退版本。

## 建议验收顺序

1. 安装集成并重启 Home Assistant，确认没有新增认证提示。
2. 添加集成，保留小数量上限并选择单独测试目录。
3. 调用 `run_backup` 且设置 `dry_run: true`，只核对固定状态与数量。
4. 选择没有敏感内容的时间段产生一个新事件，再执行一次正常备份。
5. 验证 MP4 可播放、音视频符合选项、状态不包含私有标识。
6. 观察至少一个每日计划周期后，再把目录作为正式备份目标。

## 回退

1. 在 Home Assistant 中禁用或删除本集成配置项，停止新的计划任务。
2. 删除 `config/custom_components/xiaomi_lock_cloud_backup` 后重启，即可回到安装前代码。
3. 已生成媒体不会在卸载时自动删除；由用户按自己的数据保留策略处理。
4. 不要通过回退操作删除 `hass-xiaomi-miot` 账号或认证存储。
