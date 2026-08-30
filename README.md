# Xiaomi Lock Cloud Video Backup

[![tests](https://github.com/yezhiming1/xiaomi-lock-cloud-video-backup/actions/workflows/tests.yml/badge.svg)](https://github.com/yezhiming1/xiaomi-lock-cloud-video-backup/actions/workflows/tests.yml)

Experimental Home Assistant custom integration that incrementally backs up
cloud recordings from a Xiaomi smart lock and can explicitly backfill the
currently queryable history to a user-prepared directory below `/media`.

The integration does **not** ask for Xiaomi credentials. It reuses an already
loaded in-memory cloud session from
[`hass-xiaomi-miot`](https://github.com/al-one/hass-xiaomi-miot), queries only
new recording events, decrypts AES-128 HLS segments in temporary storage, and
atomically publishes validated MP4 files.

> This project is unofficial, uses a private Xiaomi cloud API that may change,
> and is not affiliated with Xiaomi or Home Assistant.

## Status

- Candidate version: `V0.0.6` / integration manifest `0.0.6`. V0.0.5 reached
  the target, but Home Assistant's frontend treated a required numeric `0` as
  empty and rejected it, so its normal-run gate was not executed.
- Event discovery has been exercised with model `xiaomi.lock.s1`.
- Target discovery de-duplicates the same physical device across loaded cloud
  sessions. When the cloud device list has no matching model, it can fall back
  to Xiaomi Miot's loaded in-memory entity index while still requiring the
  entity's cloud object to be one of the loaded sessions.
- The encrypted-media pipeline is covered by a synthetic end-to-end fixture.
- Incremental and V0.0.3 historical real-account downloads have succeeded on
  the current target. The history service reached explicit endpoint completion;
  final history and incremental dry runs each reported zero pending items.
  Treat this release as experimental.
- V0.0.4 adds a bounded, de-identified local status journal for a separate
  recognition/operations consumer. V0.0.6 presents retention as validated
  numeric text so `0` can pass the frontend and disable downloader-owned deletion.
- Face or stranger recognition is not included in this integration.

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
- The first setup starts daily incrementals at the current time. Historical
  recordings are never backfilled automatically; the separate history service
  has its own resumable cursor and does not rewind the daily cursor.
- A history page is committed only after every event on that page is already
  handled. Empty pages follow Xiaomi's continuation marker instead of being
  treated as the end of history, and a non-decreasing marker fails closed.
- Normal non-dry runs append only a fixed state, fixed error code, attempt
  count, UTC record time, and opaque SHA-256 report key to
  `.xiaomi_lock_backup_status.jsonl`. The journal contains no device, account,
  recording, URL, credential, or media identifier.

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

Set retention to `0` when a separate verified-backup workflow owns deletion.
With `0`, normal and historical runs never perform retention deletion.

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

Historical backfill is an explicit, serialized operation:

```yaml
action: xiaomi_lock_cloud_backup.run_history_backfill
data:
  dry_run: true
  max_downloads: 100
response_variable: history_check
```

Run the service with `dry_run: false` to commit progress. Each call scans at
most 20 cloud pages and selects at most 100 recordings. Repeat only while the
fixed status is `history_limit_reached` or `history_scan_limit_reached`; stop
when `history_complete` is true. `available` is the bounded count discovered by
that call, not a promise about Xiaomi's total retention history. Historical
runs never invoke retention deletion; the configured retention policy remains
owned by the normal daily backup path.

Published files use the Xiaomi event creation time in UTC, not the download
time: `xiaomi_lock_YYYYMMDDTHHMMSSmmmZ_<digest>.mp4`. Convert the `Z` timestamp
to the desired local timezone when reading it.

Failures use fixed codes. An individual recording is retried on later runs and
quarantined after three failures so a permanently incompatible item cannot
block all newer recordings.

Infrastructure-level normal-run failures are recorded as `retrying` for the
first two consecutive failures and `failed` on the third. A successful normal
run resets that counter. Status-journal failures never expose their source
error and do not turn a successful media backup into a failed run.

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
首次配置的每日增量只从当前时间开始，默认每天本地时间 03:30 执行，保留 30 天。
历史录像不会自动下载；`run_history_backfill` 使用独立、可恢复的向过去游标，只有一页
全部处理完才提交进度，不会倒退每日增量游标。空白时间段若云端仍返回继续标记，会继续
向更早时间查找；只有接口明确结束或到达绝对时间边界才报告完成。

`V0.0.2` 会合并同一设备在多个已加载会话中的重复结果；当云设备列表没有目标型号时，
可退回到 Xiaomi Miot 已加载的内存实体索引，但实体仍必须绑定到已加载的云会话。
零个和多个不同目标会返回不同的固定错误码，响应不会包含设备编号。

`V0.0.4` 新增本地脱敏状态日志。`V0.0.5` 统一表单与后端范围，但目标前端仍把必填
数字 `0` 判为空值。`V0.0.6` 改用数字文本输入，提交后继续严格校验 `0..3650` 并
归一化为整数，使 `0` 能保存并关闭下载器删除，把删除职责交给完成远端校验的独立备份任务。
状态日志只包含固定状态、固定错误码、次数、UTC 记录时间和不可逆摘要，不包含设备、
录像、账号、URL 或认证信息。

本集成只实现增量与历史录像备份，不包含陌生人或人脸识别。目标环境已完成一次
真实历史回填、最终零待处理复核和全部目标媒体解析；私有云接口仍可能随时变化，首次
每日计划周期和长期稳定性继续保持 `UNKNOWN`。

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
