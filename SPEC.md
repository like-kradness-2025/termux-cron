# termux-cron 仕様書

Android / Termux 向け定期実行集約管理ツール。

---

## 概要

**termux-cron** は、cron が使えない Termux 環境で任意のコマンドを定周期実行するための軽量スケジューラ。
設定ファイル (YAML) と CLI の両方でタスクを管理し、実行履歴を SQLite に記録、結果を Webhook で通知できる。

---

## 決定一覧

| # | 判断 | 選んだ選択肢 |
|---|---|---|
| 1 | 言語 | Python |
| 2 | スケジュール形式 | 間隔指定 (`every 30s`, `5m`, `2h`) |
| 3 | タスク定義方法 | YAML 設定ファイル + CLI 動的追加・削除 (デーモンが 5 秒ごとに自動リロード) |
| 4 | 実行モデル | Daemon (while True + sleep loop) |
| 5 | ログ管理 | ファイルログ保存 (保持期間: 1週間) |
| 6 | ストレージ | タスク定義: YAML / 実行履歴: SQLite |
| 7 | 通知 | Webhook サポート |
| 8 | スコープ | 純スケジューラ (監視機能は内蔵しない) |

---

## 機能仕様

### 1. タスク定義 (YAML)

デフォルトパス: `~/.config/termux-cron/tasks.yaml`

```yaml
# tasks.yaml
tasks:
  - name: system-monitor
    cmd: python3 /home/termux/termux-cron/collector.py
    every: 1s
    enabled: true

  - name: render-dashboard
    cmd: python3 /home/termux/termux-cron/dashboard/render.py
    every: 1m
    enabled: true
    webhook: https://discord.com/api/webhooks/...

  - name: backup-logs
    cmd: tar czf /sdcard/backup/logs.tar.gz logs/
    every: 6h
    enabled: false
```

各フィールド:

| フィールド | 必須 | 型 | 説明 |
|---|---|---|---|
| `name` | yes | str | タスク名 (一意) |
| `cmd` | yes | str | 実行するコマンド (shell) |
| `every` | yes | str | 間隔 (`30s`, `5m`, `1h`, `1d` など) |
| `enabled` | no | bool | 有効/無効 (デフォルト: true) |
| `webhook` | no | str | 結果送信先 Webhook URL |
| `timeout` | no | str | タイムアウト (例: `10m`, 超えると強制終了) |
| `cwd` | no | str | 実行ディレクトリ |

`every` のパースルール:

| 表記 | 意味 |
|---|---|
| `30s` | 30秒 |
| `1m` / `5m` / `30m` | 1分 / 5分 / 30分 |
| `1h` / `6h` | 1時間 / 6時間 |
| `1d` | 1日 |

### 2. CLI コマンド

```
Usage: termux-cron [command] [options]

Commands:
  daemon             起動 (foreground)
  add <name>         タスク追加
    --every <interval>
    --cmd <command>
    --webhook <url>
    --timeout <duration>
    --cwd <path>
  remove <name>      タスク削除
  list               タスク一覧表示
  enable <name>      タスク有効化
  disable <name>     タスク無効化
  logs <name>        タスクのログ表示
    --tail <N>       (最後のN行, default: 50)
    --since <time>   (指定時刻以降)
  history <name>     タスクの実行履歴表示
    --limit <N>
  status             デーモンの状態表示
```

### 3. Daemon 動作

```
while True:
    for each task:
        if task.enabled and task.due():
            run task.cmd in subprocess
            capture stdout/stderr
            log to file (logs/<task_name>/YYYY-MM-DD.log)
            record run in SQLite (ts, exit_code, duration)
            if webhook set: POST result to webhook
    sleep 1
```

- **最小 tick**: 1秒
- **並列実行**: タスクはスレッドプールで並列実行される。長時間タスクが他のタスクの tick をブロックすることはない。同一タスクの重複実行は防止される。
- **タイムアウト**: subprocess に timeout を設定。超過時は kill + エラー記録

### 4. ログ

- パス: `logs/<task_name>/YYYY-MM-DD.log`
- 保持期間: **7日間 (1週間)**
- daemon 起動時および毎時 (3600秒ごと) に 7日より古いログファイルを削除
- ログローテーションは日次 (日付跨ぎで新ファイル)

### 5. SQLite 実行履歴

DB パス: `~/.config/termux-cron/history.db`

```sql
CREATE TABLE runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name   TEXT NOT NULL,
    started_at  TEXT NOT NULL,  -- ISO8601
    finished_at TEXT,
    exit_code   INTEGER,
    duration_ms INTEGER,
    output      TEXT,           -- stdout + stderr (直近のみ保存)
    webhook_ok  INTEGER         -- 0/1/null (webhook送信結果)
);

CREATE INDEX idx_runs_task ON runs(task_name, started_at);
```

- 直近の output は保持 (古い run の output は NULL 化して容量節減)
- 履歴は自動削除しない (必要なら手動で cleanup)

### 6. Webhook

POST 先: `task.webhook` (tasks.yaml で設定)

リクエストボディ (JSON):

```json
{
  "task": "system-monitor",
  "started_at": "2026-06-04T12:00:00Z",
  "finished_at": "2026-06-04T12:00:01Z",
  "exit_code": 0,
  "duration_ms": 1234,
  "output": "{\"ok\": true, ...}"
}
```

Content-Type: `application/json`

---

## ディレクトリ構造

```
~/.config/termux-cron/
├── tasks.yaml          # タスク定義
└── history.db          # 実行履歴 (SQLite)

~/termux-cron/
├── termux-cron.py      # CLI エントリポイント
├── core/
│   ├── __init__.py
│   ├── daemon.py       # メインループ
│   ├── scheduler.py    # タスクスケジューリング
│   ├── runner.py       # コマンド実行・タイムアウト管理
│   ├── config.py       # YAML 読み書き
│   ├── storage.py      # SQLite 操作
│   └── webhook.py      # Webhook POST
├── logs/               # 実行ログ (タスク名/日付.log)
├── tasks.yaml.example  # サンプル設定
└── README.md
```

---

## 非機能要件

- **軽量**: アイドル時のメモリ使用量 50MB 以下
- **依存最小**: Python 標準ライブラリ + PyYAML のみ (他はオプション)
- **シグナルハンドリング**: SIGTERM/SIGINT で graceful shutdown
- **起動時リカバリ**: 前回 daemon 実行中に落ちたタスクの状態は破棄 (resume しない)
