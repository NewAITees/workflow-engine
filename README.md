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
2. **Worker Agent**: `status:ready` Issue → テスト生成 → 実装 → テスト実行 → PR作成
3. **Reviewer Agent**: `status:reviewing` PR → レビュー → Approve/Request Changes

### Worker Agentの詳細フロー（TDD）

```
status:ready
    ↓
status:implementing（テスト生成）
    ↓
status:implementing（実装生成）
    ↓
status:testing（pytest実行）
    ↓ (成功)      ↓ (失敗、最大3回再試行)
status:reviewing   status:implementing（再実装）
    ↓
PR作成
```

## 主要機能

### ✅ TDD（テスト駆動開発）徹底

Worker Agentは厳格なTDDプロセスに従います：

1. **テスト生成優先**: LLMが仕様からpytestテストを先に生成
2. **実装生成**: テストをパスする実装をLLMが生成
3. **自動テスト実行**: Workerがpytestを自動実行
4. **失敗時の自動再試行**: テスト失敗時、失敗出力をLLMに渡して再実装（最大3回）
5. **全テストパス後にPR作成**: テストが通るまでPRを作成しない

**メリット:**
- コード品質の自動保証
- レビューサイクルの削減
- バグの早期発見
- 透明な進捗追跡（GitHub Issue/PRコメント）

### 🔄 クラッシュ耐性・自動再開

各エージェントには一意のIDが付与され、クラッシュからの自動復旧が可能：

1. **エージェントID**: 各インスタンスに一意のID（例: `worker-a1b2c3d4`）
2. **30分タイムアウト**: ロックは30分後に自動失効
3. **自動再開**: 別のエージェントがタイムアウト後に自動的に作業を引き継ぎ
4. **担当者の可視化**: ACKコメントでどのエージェントが処理中か確認可能

**ユースケース:**
- Workerクラッシュ時に別のWorkerが自動再開
- 長時間停滞しているタスクの自動再取得
- 複数Workerの並行稼働

### 🔧 CI失敗自動修正

PR作成後、Worker AgentはCI状態を監視し、失敗時に自動で修正を試みます：

1. **CI監視**: PR作成後、CI完了まで自動待機（最大10分）
2. **失敗検出**: CIチェックが失敗したら詳細ログを取得
3. **自動修正ループ**: LLMがログを分析→修正生成→プッシュ→CI再実行（最大3回）
4. **失敗時の処理**: 全リトライ失敗時は`status:ci-failed`ラベルを付与して通知

**メリット:**
- CI失敗の手動対応を削減
- 高速な反復サイクル
- CI logsをLLMに渡して文脈を提供
- ラベルとコメントで明確な失敗追跡

**設定:**
- `MAX_CI_RETRIES`: 自動修正試行回数（デフォルト: 3回）
- `CI_WAIT_TIMEOUT`: CI待機タイムアウト（デフォルト: 10分）
- `CI_CHECK_INTERVAL`: CIポーリング間隔（デフォルト: 30秒）

## 必要要件

- Python 3.11+
- [Codex CLI](https://github.com/openai/codex) (`codex` コマンド) - デフォルト
- [Claude Code CLI](https://claude.ai/code) (`claude` コマンド) - オプション
- [GitHub CLI](https://cli.github.com/) (`gh` コマンド)
- Git
- pytest（テスト実行用）

## セットアップ

```bash
# 1. GitHub CLIでログイン
gh auth login

# 2. Codex CLIが使えることを確認（デフォルト）
codex --version

# または Claude Code CLI
claude --version

# 3. 依存関係のインストール
uv sync --all-extras

# 4. pre-commitのセットアップ（開発者向け）
uv run pre-commit install

# 5. 設定ファイルをコピー
cp config/repos.yml.example config/repos.yml

# 6. 設定を編集
# repos.yml でリポジトリとLLMバックエンドを設定
```

## 開発者向けコマンド

```bash
# テスト実行
uv run pytest

# カバレッジ付きテスト
uv run pytest --cov=shared --cov=planner-agent --cov=worker-agent --cov=reviewer-agent

# リント
uv run ruff check .

# フォーマット
uv run ruff format .

# 型チェック
uv run mypy .

# pre-commitを全ファイルに実行
uv run pre-commit run --all-files
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

## 自動マージ機能

Reviewer Agentが`status:approved`を付与した直後に、自動でマージを試行できます。
リポジトリごとにopt-inで有効化します。

```yaml
repositories:
  - name: owner/repo
    auto_merge: true        # デフォルト: false
    merge_method: squash    # "squash" (デフォルト), "merge", "rebase"
```

- `auto_merge`: `true`で自動マージを有効化（デフォルト: `false`）
- `merge_method`: マージ方法を指定
  - `squash`: 全コミットを1つにまとめる（推奨）
  - `merge`: マージコミットを作成
  - `rebase`: リベースしてマージ

## ラベル体系

| ラベル | 意味 |
|--------|------|
| `status:ready` | 実装準備完了（Worker待ち） |
| `status:implementing` | 実装中（Workerがテスト生成・実装中） |
| `status:testing` | テスト実行中（Workerがpytest実行中） |
| `status:reviewing` | レビュー待ち（Reviewer待ち、CI通過後） |
| `status:in-review` | レビュー中（Reviewerがロック中） |
| `status:approved` | レビュー承認済み |
| `status:changes-requested` | 修正要求 |
| `status:ci-failed` | CI失敗（自動修正3回失敗後） |
| `status:failed` | 処理失敗 |

**ラベル遷移フロー:**
```
ready → implementing → testing → CI監視 → reviewing → in-review → approved
                ↑           ↓       ↓ (失敗)              ↓
                └───(retry)─┘    ci-failed    changes-requested
```

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

複数エージェント間の競合を防ぎ、クラッシュからの復旧を可能にするロック機構。

**ロック取得フロー:**
1. **既存ロック確認**: 30分以内のACKコメントがあるかチェック
   - あり → スキップ（他のエージェントが処理中）
   - なし → 次へ
2. **ACKコメント投稿**: `ACK:worker:agent-id:timestamp` 形式で投稿
3. **2秒待機**: 競合検出のための待機
4. **競合解決**: 30秒以内のACKのうち最古のタイムスタンプが勝者
5. **ラベル遷移実行**: 勝者がラベルを変更

**タイムアウトと復旧:**
- **30分タイムアウト**: ACKから30分経過したロックは無効
- **自動再開**: 別のエージェントがタイムアウト後に自動取得
- **エージェントID追跡**: 各エージェントは一意のID（例: `worker-a1b2c3d4`）

**ACKコメント例:**
```
ACK:worker:worker-a1b2c3d4:1706123456789
```

これにより、エージェントクラッシュ時も30分後には自動的に別のエージェントが作業を再開できます。

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
