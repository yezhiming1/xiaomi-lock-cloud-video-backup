# 当前状态

- 项目版本：`V0.0.1`
- 集成域：`xiaomi_lock_cloud_backup`
- 发布级别：公开实验版候选
- 目标型号：默认 `xiaomi.lock.s1`
- 数据边界：只复用已加载的 `hass-xiaomi-miot` 内存会话
- 历史回填：关闭；首次配置从当前时间建立游标
- 默认计划：Home Assistant 本地时间每日 `03:30:00`
- 默认保留：30 天
- 陌生人识别：不在本版本范围内

本地纯测试和固定 Home Assistant 镜像导入结果记录在 `RELEASE.md`。真实账号媒体下载、
NAS 持久化和长期定时运行在各自目标环境验证前保持 `UNKNOWN`。
