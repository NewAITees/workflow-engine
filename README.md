# AI Workflow Engine

GitHubをメッセージキューとして使用する、3エージェント構成の自律型開発ワークフローエンジン。

## アーキテクチャ

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Planner   │    │   Worker    │    │  Reviewer   │
│    Agent    │    │    Agent    │    │    Agent    │
└──────┬──────┘    └──────┬──────┘    └──────┬──────┘
       │                  │                  │
       └──────────────────┼──────────────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │   GitHub Repository   │
              │  (Issues, PRs, Labels)│
              └───────────────────────┘
```

## ワークフロー

1. **Planner Agent**: ユーザーストーリー → 仕様書（Issue）
2. **Worker Agent**: `status:ready` Issue → 実装 → PR作成
3. **Reviewer Agent**: `status:reviewing` PR → レビュー → Approve/Request Changes

## 必要要件

- Python 3.11+
- [Codex CLI](https://github.com/openai/codex) (`codex` コマンド) - デフォルト
- [Claude Code CLI](https://claude.ai/code) (`claude` コマンド) - オプション
- [GitHub CLI](https://cli.github.com/) (`gh` コマンド)
- Git

## セットアップ

```bash
# 1. GitHub CLIでログイン
gh auth login

# 2. Codex CLIが使えることを確認（デフォルト）
codex --version

# または Claude Code CLI
claude --version

# 3. 設定ファイルをコピー
cp config/repos.yml.example config/repos.yml

# 4. 設定を編集
# repos.yml でリポジトリとLLMバックエンドを設定
```

## LLMバックエンド設定

`config/repos.yml` で `llm_backend` を設定:

```yaml
repositories:
  - name: owner/repo
    llm_backend: codex    # "codex" (デフォルト) または "claude"
    codex_cli: codex      # Codex CLIのパス
    claude_cli: claude    # Claude Code CLIのパス
```

| バックエンド | CLI | 説明 |
|-------------|-----|------|
| `codex` | `codex` | OpenAI Codex（デフォルト） |
| `claude` | `claude` | Anthropic Claude Code |

## 使い方

### Planner Agent（インタラクティブ）

```bash
# 対話モードで仕様作成
uv run planner-agent/main.py owner/repo

# 非対話モードで仕様作成
uv run planner-agent/main.py owner/repo --story "ユーザー検索機能を追加"
```

### Worker Agent（デーモン）

```bash
# デーモンモードで起動（常駐）
uv run worker-agent/main.py owner/repo

# 一回だけ実行（テスト用）
uv run worker-agent/main.py owner/repo --once

# デバッグ出力
uv run worker-agent/main.py owner/repo --verbose
```

### Reviewer Agent（デーモン）

```bash
# デーモンモードで起動（常駐）
uv run reviewer-agent/main.py owner/repo

# 一回だけ実行（テスト用）
uv run reviewer-agent/main.py owner/repo --once
```

## Reviewer Agent 自動マージ

Reviewer Agent が `status:approved` を付与した直後に、自動でマージを試行します。
リポジトリごとに opt-in で有効化できます。

```yaml
repositories:
  - name: owner/repo
    auto_merge: true        # デフォルト: false
    merge_method: squash    # "squash" (デフォルト), "merge", "rebase"
```

## ラベル体系

| ラベル | 意味 |
|--------|------|
| `status:ready` | 実装準備完了（Worker待ち） |
| `status:implementing` | 実装中（Workerがロック中） |
| `status:reviewing` | レビュー待ち（Reviewer待ち） |
| `status:in-review` | レビュー中（Reviewerがロック中） |
| `status:approved` | レビュー承認済み |
| `status:changes-requested` | 修正要求 |
| `status:failed` | 処理失敗 |

## ディレクトリ構成

```
workflow-engine/
├── shared/              # 共通モジュール
│   ├── github_client.py # GitHub API (gh CLI wrapper)
│   ├── llm_client.py    # LLM統一クライアント (codex/claude)
│   ├── lock.py          # 分散ロック機構
│   └── config.py        # 設定管理
├── planner-agent/       # Planner エージェント
│   └── main.py
├── worker-agent/        # Worker エージェント
│   ├── main.py
│   └── git_operations.py
├── reviewer-agent/      # Reviewer エージェント
│   └── main.py
└── config/
    └── repos.yml        # 設定ファイル
```

## ロック機構

複数エージェント間の競合を防ぐため、ACKコメント + ラベル遷移による楽観的ロックを使用。

1. ACKコメント投稿（タイムスタンプ付き）
2. 2秒待機（競合検出）
3. 最初のACKが自分か確認（30秒以内のACKのみ有効）
4. ラベル遷移実行

## トラブルシューティング

### Issue/PRが処理されない

1. ラベルが正しいか確認
2. `--verbose` オプションでログ確認
3. `gh auth status` で認証確認

### LLMがタイムアウト

- 大きなリポジトリでは時間がかかる
- `shared/llm_client.py` の timeout 値を調整

### ロック競合が頻発

- `poll_interval` を調整して負荷分散
- 複数Worker/Reviewerの場合は間隔をずらす

## Systemdでの常駐化

```ini
# /etc/systemd/system/workflow-worker@.service
[Unit]
Description=Workflow Worker Agent for %i
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/workflow-engine
ExecStart=/usr/bin/python3 worker-agent/main.py %i
Restart=always
RestartSec=60
Environment=WORKFLOW_CONFIG=/path/to/repos.yml

[Install]
WantedBy=multi-user.target
```

```bash
# 有効化
sudo systemctl enable workflow-worker@owner-repo
sudo systemctl start workflow-worker@owner-repo
```

## ライセンス

MIT
