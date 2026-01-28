# Workflow Engine E2Eテスト引継ぎ資料

## 概要
workflow-engineを使ってステータスダッシュボード機能を自動実装するE2Eテストを実施中。
Windowsでcodex CLIのサンドボックス制限が効かない問題があり、WSLでの続行を推奨。

## 現在の状況

### 完了した作業
1. **ラベル作成済み** - GitHubリポジトリに以下のラベルを作成:
   - `status:ready`, `status:implementing`, `status:reviewing`
   - `status:in-review`, `status:approved`, `status:failed`

2. **設定ファイル作成済み** - `workflow-engine/config/repos.yml`
   ```yaml
   repositories:
     - name: NewAITees/aituber_test
       poll_interval: 30
       llm_backend: codex
   ```

3. **Issue作成済み** - Issue #1 (status:failedになっている)
   - https://github.com/NewAITees/aituber_test/issues/1
   - ステータスダッシュボード機能の仕様

4. **コード修正済み**:
   - `shared/config.py`: `~`をホームディレクトリに展開するよう修正
   - `shared/github_client.py`: `encoding="utf-8"`追加（日本語対応）
   - `shared/llm_client.py`: `codex exec`コマンド形式に修正、`encoding="utf-8"`追加

### 未解決の問題
- **Windowsでcodex execのサンドボックス設定が効かない**
  - `--sandbox workspace-write`や`--full-auto`を指定しても`read-only`のまま
  - `--dangerously-bypass-approvals-and-sandbox`なら動作するがセキュリティリスクあり

## WSLでの続行手順

### 1. 事前準備
```bash
# WSLにcodex CLIをインストール
npm install -g @anthropic-ai/codex

# GitHub CLIをインストール・ログイン
sudo apt install gh
gh auth login

# リポジトリをクローン
git clone https://github.com/NewAITees/aituber_test.git
cd aituber_test/workflow-engine
```

### 2. 設定ファイル更新
`config/repos.yml`のパスをLinux形式に変更:
```yaml
repositories:
  - name: NewAITees/aituber_test
    poll_interval: 30
    work_dir: ~/.workflow-engine/workspaces
    llm_backend: codex
    codex_cli: codex
    gh_cli: gh
```

### 3. Issue #1のラベルをリセット
```bash
gh issue edit 1 --repo NewAITees/aituber_test --remove-label status:failed --add-label status:ready
```

### 4. エージェント起動
```bash
# ターミナル1: Worker Agent
cd workflow-engine
uv run worker-agent/main.py NewAITees/aituber_test --verbose

# ターミナル2: Reviewer Agent
uv run reviewer-agent/main.py NewAITees/aituber_test --verbose
```

### 5. 動作確認
Worker Agentが以下を行うはず:
1. Issue #1を検出 (`status:ready`)
2. ラベルを`status:implementing`に変更
3. リポジトリをクローン
4. `auto/issue-1`ブランチ作成
5. codexで実装生成
6. コミット・プッシュ
7. PR作成 (`status:reviewing`ラベル付き)

Reviewer Agentが:
1. PRを検出 (`status:reviewing`)
2. レビュー実行
3. Approve/Request Changes

## 実装対象の機能

### ステータスダッシュボード (Issue #1)
- ファイル: `workflow-engine/scripts/status.py`
- 機能: リポジトリのIssue/PRをワークフローステータスごとに表示
- 使い方: `uv run scripts/status.py owner/repo [--json]`

仕様詳細はIssue #1を参照:
https://github.com/NewAITees/aituber_test/issues/1

## 変更したファイル一覧

| ファイル | 変更内容 |
|---------|---------|
| `shared/config.py` | `expanduser()`で`~`を展開 |
| `shared/github_client.py` | `encoding="utf-8"`追加 |
| `shared/llm_client.py` | `codex exec`形式、`encoding="utf-8"`追加 |
| `config/repos.yml` | 新規作成（テスト用設定） |
| `~/.codex/config.toml` | `sandbox_mode`設定追加（Windows用、効果なし） |

## 注意事項

1. **codexのサンドボックス**: WSLではLinuxのサンドボックスが使えるので`workspace-write`が機能するはず
2. **MCPサーバー**: `~/.codex/config.toml`のMCPサーバー設定はコメントアウト済み
3. **エンコーディング**: 日本語タイトルのIssueがあるのでUTF-8設定必須

## 参考リンク

- [Codex Security Documentation](https://developers.openai.com/codex/security/)
- [Codex CLI Reference](https://developers.openai.com/codex/cli/reference/)
- [GitHub Issue #1](https://github.com/NewAITees/aituber_test/issues/1)
