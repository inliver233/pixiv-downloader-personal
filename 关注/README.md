# 关注迁移（导出 + 自动关注）

目标：把旧账号的「关注列表」从本项目数据库里导出来（txt，一行一个用户ID），然后用新账号的 `refresh_token` 登录并逐个关注，实现关注迁移。

> 注意：批量关注属于自动化行为，过快可能触发风控/限制。建议设置合理延迟、分批执行。

---

## 1) 从数据库导出关注列表

默认会读取本项目根目录的 `db.sqlite`，导出到 `关注/following_member_ids.txt`：

```powershell
python .\关注\export_following_from_db.py
```

自定义数据库路径/输出路径：

```powershell
python .\关注\export_following_from_db.py --db .\db.sqlite --out .\关注\following_member_ids.txt
```

如果你想顺便导出名字（仍然是一行一个，字段用 TAB 分隔）：

```powershell
python .\关注\export_following_from_db.py --with-name --out .\关注\following_member_ids_with_name.txt
```

---

## 1.5) 直接用 refresh_token 在线导出关注列表（不依赖数据库）

会用 `config.ini` 里的 `[Authentication] refresh_token` 登录当前账号，然后把关注列表导出到 `关注/following_member_ids.txt`：

```powershell
python .\关注\export_following_from_token.py
```

（可选）如果你不想写进 `config.ini`，也可以命令行传入：

```powershell
python .\关注\export_following_from_token.py --refresh-token "YOUR_REFRESH_TOKEN"
```

---

## 2) 用 refresh_token 自动关注 txt 里的用户

推荐直接在命令行传入新账号的 `refresh_token`（避免写进 `config.ini`）：

```powershell
python .\关注\auto_follow_from_txt.py --refresh-token "YOUR_REFRESH_TOKEN" --input .\关注\following_member_ids.txt
```

默认行为：
- `public` 关注（可用 `--restrict private` 改为悄悄关注）
- 每次关注后随机 sleep 1.0~2.5 秒（可用 `--min-sleep/--max-sleep` 调整）
- 断点续跑：成功的ID会写入 `关注/follow_done.txt`，下次运行会自动跳过
- 失败的会记录在 `关注/follow_failed.txt`；过程日志写入 `关注/auto_follow_log.jsonl`

常用参数：

```powershell
# 悄悄关注 + 慢一点 + 每次最多跑 200 个（分批）
python .\关注\auto_follow_from_txt.py --refresh-token "YOUR_REFRESH_TOKEN" `
  --restrict private --min-sleep 2.0 --max-sleep 5.0 --limit 200

# dry-run：只打印将要处理的 ID，不发请求
python .\关注\auto_follow_from_txt.py --input .\关注\following_member_ids.txt --dry-run
```

---

## 关于 refresh_token

本项目本身支持 `refresh_token` 登录（见根目录 `config.ini` 的 `[Authentication] refresh_token`）。

本工具默认会加载根目录 `config.ini` 读取代理/SSL 等设置；`refresh_token` 优先使用 `--refresh-token` 参数，其次才读取 `config.ini`。
