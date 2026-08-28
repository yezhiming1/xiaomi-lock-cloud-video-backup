# Xiaomi Lock Cloud Video Backup

[![tests](https://github.com/yezhiming1/xiaomi-lock-cloud-video-backup/actions/workflows/tests.yml/badge.svg)](https://github.com/yezhiming1/xiaomi-lock-cloud-video-backup/actions/workflows/tests.yml)

Experimental Home Assistant custom integration that incrementally backs up
cloud recordings from a Xiaomi smart lock to a user-prepared directory below
`/media`.

The integration does **not** ask for Xiaomi credentials. It reuses an already
loaded in-memory cloud session from
[`hass-xiaomi-miot`](https://github.com/al-one/hass-xiaomi-miot), queries only
new recording events, decrypts AES-128 HLS segments in temporary storage, and
atomically publishes validated MP4 files.

> This project is unofficial, uses a private Xiaomi cloud API that may change,
> and is not affiliated with Xiaomi or Home Assistant.

## Status

- Version: `V0.0.2` / integration manifest `0.0.2`
- Event discovery has been exercised with model `xiaomi.lock.s1`.
- Target discovery de-duplicates the same physical device across loaded cloud
  sessions. When the cloud device list has no matching model, it can fall back
  to Xiaomi Miot's loaded in-memory entity index while still requiring the
  entity's cloud object to be one of the loaded sessions.
- The encrypted-media pipeline is covered by a synthetic end-to-end fixture.
- A real-account media download and long-running production deployment remain
  unverified. Treat this release as experimental.
- Face or stranger recognition is not included in `V0.0.2`.

## Safety properties

- No password, cookie, Home Assistant token, or Xiaomi auth file is read,
  exported, or persisted by this integration.
- Device IDs, file IDs, signed media URLs, keys, and IVs stay in process memory.
- Persisted event identities are SHA-256 digests; diagnostics contain only
  counts, booleans, and fixed status codes.
- `ffmpeg` and `ffprobe` receive local paths only. Signed URLs and AES material
  never appear in their command lines.
- Output is confined below `/media`. Retention deletes only validated regular
  files recorded in this integration's state and never follows symlinks.
- The first setup starts at the current time. Historical recordings are not
  backfilled automatically.

## Requirements

- Home Assistant `2026.8.2` is the pinned validation target. Other versions are
  currently unverified.
- [`hass-xiaomi-miot`](https://github.com/al-one/hass-xiaomi-miot) `1.1.4` with
  a working Xiaomi cloud session already loaded in Home Assistant.
- `ffmpeg` and `ffprobe` available inside the Home Assistant runtime.
- An existing writable parent beneath `/media`. Configure any network storage
  in Home Assistant before adding this integration; this project never creates
  or mounts a NAS share.
- The target filesystem must support same-directory hard links and `fsync`,
  which are used to publish a completed file without ever overwriting an
  existing path. Unsupported storage fails with a fixed code and keeps the
  destination untouched.

The official Xiaomi Home integration alone does not expose the in-memory
session interface used here.

## Install

### HACS custom repository

1. In HACS, add
   `https://github.com/yezhiming1/xiaomi-lock-cloud-video-backup` as an
   **Integration** custom repository.
2. Download **Xiaomi Lock Cloud Video Backup** and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration** and select the
   integration.
4. Keep the default model only if the target is `xiaomi.lock.s1`. Choose an
   output subdirectory whose parent already exists below `/media`.

### Manual installation

Copy `custom_components/xiaomi_lock_cloud_backup` into Home Assistant's
`config/custom_components` directory, restart Home Assistant, then add the
integration from the UI.

## Operation

Defaults:

- Daily schedule: `03:30:00` in Home Assistant's local timezone
- Output leaf: `/media/xiaomi_lock_cloud_backup`
- Retention: 30 days
- Maximum downloads per run: 100
- Audio: retained when present

The service `xiaomi_lock_cloud_backup.run_backup` supports a safe discovery
check:

```yaml
action: xiaomi_lock_cloud_backup.run_backup
data:
  dry_run: true
response_variable: backup_check
```

A dry run may contact Xiaomi's event-list API, but it does not download media,
delete files, or update backup state. A normal run uses `dry_run: false`.

Failures use fixed codes. An individual recording is retried on later runs and
quarantined after three failures so a permanently incompatible item cannot
block all newer recordings.

The target model and output directory are fixed when the entry is created so
retention authority cannot silently move to a different directory. To change
either value, remove and recreate the entry; existing media is left untouched.

## 中文说明

这是一个实验性的 Home Assistant 自定义集成，用于把小米智能门锁的新云录像
增量备份到用户预先准备好的 `/media` 子目录。它不会让你再次输入米家账号，
而是只复用已经运行的 `hass-xiaomi-miot` 内存会话；也不会读取其认证文件。

安装前请先在 Home Assistant 中准备好可写媒体目录。若目标是 NAS，网络存储应由
用户先在 Home Assistant 中挂载，本集成不会创建共享、修改挂载或探测 NAS 凭据。
目标文件系统还必须支持同目录硬链接和 `fsync`，用于无覆盖原子发布；不支持时会以
固定错误码停止，不会退化为覆盖已有文件。
首次配置只从当前时间开始，默认每天本地时间 03:30 执行，保留 30 天。

`V0.0.2` 会合并同一设备在多个已加载会话中的重复结果；当云设备列表没有目标型号时，
可退回到 Xiaomi Miot 已加载的内存实体索引，但实体仍必须绑定到已加载的云会话。
零个和多个不同目标会返回不同的固定错误码，响应不会包含设备编号。

`V0.0.2` 只实现录像备份，不包含陌生人或人脸识别。私有云接口可能随时变化，
真实设备下载和长期运行仍需要在你自己的环境中谨慎验证。

## Development

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
./scripts/audit-public-tree.ps1
./scripts/test-in-home-assistant.ps1
```

The last command imports the integration using a digest-pinned Home Assistant
`2026.8.2` container with container networking disabled.

See [security design](docs/SECURITY.md), [architecture](docs/ARCHITECTURE.md),
and [release evidence](docs/RELEASE.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
