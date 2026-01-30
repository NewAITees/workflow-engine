# Phase B & C 実装仕様書

## 概要

このドキュメントは、AI Workflow EngineのPhase B（Reviewer重大度ベース対応）とPhase C（Plannerフィードバックループ）の詳細実装仕様を記載しています。

**前提条件：Phase A（CI自動修正）が完了済み**

- CI失敗時の自動修正ループ実装済み
- `status:ci-failed`ラベル追加済み
- CI監視・ログ取得機能実装済み

## Phase B: Reviewer重大度ベース対応

### 目的

Reviewerがコードレビュー時に発見した問題の重大度を判定し、軽微な修正は蓄積してまとめて返す仕組みを実装する。

### 背景

現在のReviewerは、どんな小さな問題でも即座に`status:changes-requested`を付与してWorkerに戻してしまう。これにより：
- 軽微な指摘（typo、コメント、フォーマット等）でもPRが差し戻される
- レビューサイクルが長くなる
- Workerの負荷が高まる

### 解決策

**重大度判定システム**を導入し、軽微な問題は蓄積、重大な問題のみ即座に差し戻す。

---

## Phase B 詳細仕様

### B-1: 重大度分類定義

```python
# reviewer-agent/main.py に追加

class IssueSeverity(Enum):
    """Issue severity classification."""
    CRITICAL = "critical"      # セキュリティ、データ損失、クラッシュ
    MAJOR = "major"            # 機能不全、パフォーマンス問題
    MINOR = "minor"            # コードスタイル、リファクタリング推奨
    TRIVIAL = "trivial"        # typo、コメント、ドキュメント
```

**判定基準:**

| 重大度 | 説明 | 例 |
|--------|------|-----|
| CRITICAL | セキュリティ脆弱性、データ損失リスク、クラッシュ原因 | SQLインジェクション、NullPointerException、メモリリーク |
| MAJOR | 機能が動作しない、パフォーマンス問題 | ロジックエラー、無限ループ、N+1クエリ |
| MINOR | 動作するが改善推奨、コードスタイル違反 | 命名規約違反、冗長コード、マジックナンバー |
| TRIVIAL | 軽微なドキュメント・コメント修正 | typo、コメント不足、docstring欠落 |

### B-2: LLMへの重大度判定プロンプト

```python
# shared/llm_client.py に追加

def review_code_with_severity(
    self,
    spec: str,
    diff: str,
    repo_context: str,
    work_dir: Path,
) -> LLMResult:
    """
    Review code and classify issues by severity.

    Returns:
        LLMResult with structured review in JSON format:
        {
            "overall_decision": "approve" | "request_changes",
            "issues": [
                {
                    "severity": "critical" | "major" | "minor" | "trivial",
                    "file": "path/to/file.py",
                    "line": 42,
                    "description": "Issue description",
                    "suggestion": "How to fix"
                }
            ],
            "summary": "Overall review summary"
        }
    """

    prompt = f"""You are reviewing a pull request implementation.

## Specification
{spec}

## Code Changes (diff)
{diff}

## Repository Context
{repo_context}

## Instructions

1. Review the code changes against the specification
2. Identify issues and classify each by severity:
   - **CRITICAL**: Security vulnerabilities, data loss risks, crashes
   - **MAJOR**: Functional failures, performance problems
   - **MINOR**: Code style violations, refactoring recommendations
   - **TRIVIAL**: Typos, comments, documentation

3. Return a JSON response with this structure:
```json
{{
    "overall_decision": "approve or request_changes",
    "issues": [
        {{
            "severity": "critical|major|minor|trivial",
            "file": "path/to/file.py",
            "line": 42,
            "description": "Clear description of the issue",
            "suggestion": "How to fix it"
        }}
    ],
    "summary": "Brief overall assessment"
}}
```

## Decision Rules
- If ANY critical or major issues exist: overall_decision = "request_changes"
- If ONLY minor/trivial issues exist: overall_decision = "approve"
- If NO issues: overall_decision = "approve"

Start your review."""

    return self._run(
        prompt,
        work_dir,
        allowed_tools=["Read", "Grep", "Glob"],
    )
```

### B-3: 軽微な問題の蓄積ストレージ

**ストレージ場所:** `~/.workflow-engine/accumulated_fixes/<repo-name>/`

**ファイル構造:**
```
~/.workflow-engine/accumulated_fixes/owner-repo/
├── pr-42.json          # PR #42の蓄積された軽微な問題
├── pr-43.json
└── metadata.json       # メタ情報
```

**pr-{number}.json フォーマット:**
```json
{
    "pr_number": 42,
    "issue_number": 123,
    "created_at": "2025-01-31T10:00:00Z",
    "last_updated": "2025-01-31T12:00:00Z",
    "accumulated_issues": [
        {
            "review_id": "review-abc123",
            "timestamp": "2025-01-31T10:00:00Z",
            "severity": "minor",
            "file": "src/auth.py",
            "line": 42,
            "description": "Variable name should be snake_case",
            "suggestion": "Rename 'userName' to 'user_name'"
        },
        {
            "review_id": "review-def456",
            "timestamp": "2025-01-31T12:00:00Z",
            "severity": "trivial",
            "file": "src/auth.py",
            "line": 10,
            "description": "Typo in docstring",
            "suggestion": "Change 'athentication' to 'authentication'"
        }
    ],
    "threshold": 5,
    "current_count": 2
}
```

### B-4: Reviewer Agent改修

**ファイル:** `reviewer-agent/main.py`

**追加メソッド:**

```python
class ReviewerAgent:

    def __init__(self, repo: str):
        # ...existing code...
        self.accumulated_fixes_dir = Path.home() / ".workflow-engine" / "accumulated_fixes" / repo.replace("/", "-")
        self.accumulated_fixes_dir.mkdir(parents=True, exist_ok=True)
        self.ACCUMULATED_THRESHOLD = 5  # 蓄積5件でまとめて返す

    def _load_accumulated_fixes(self, pr_number: int) -> dict:
        """Load accumulated fixes for a PR."""
        fix_file = self.accumulated_fixes_dir / f"pr-{pr_number}.json"
        if fix_file.exists():
            with open(fix_file, "r") as f:
                return json.load(f)
        return {
            "pr_number": pr_number,
            "issue_number": None,
            "created_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
            "accumulated_issues": [],
            "threshold": self.ACCUMULATED_THRESHOLD,
            "current_count": 0,
        }

    def _save_accumulated_fixes(self, pr_number: int, data: dict) -> None:
        """Save accumulated fixes for a PR."""
        data["last_updated"] = datetime.now().isoformat()
        fix_file = self.accumulated_fixes_dir / f"pr-{pr_number}.json"
        with open(fix_file, "w") as f:
            json.dump(data, f, indent=2)

    def _add_accumulated_issue(self, pr_number: int, issue: dict) -> bool:
        """
        Add a minor/trivial issue to accumulated fixes.

        Returns:
            True if threshold reached (should send feedback now)
            False if still accumulating
        """
        data = self._load_accumulated_fixes(pr_number)

        issue["review_id"] = f"review-{uuid.uuid4().hex[:8]}"
        issue["timestamp"] = datetime.now().isoformat()

        data["accumulated_issues"].append(issue)
        data["current_count"] = len(data["accumulated_issues"])

        self._save_accumulated_fixes(pr_number, data)

        # Check threshold
        return data["current_count"] >= data["threshold"]

    def _format_accumulated_feedback(self, pr_number: int) -> str:
        """Format accumulated issues as feedback message."""
        data = self._load_accumulated_fixes(pr_number)

        if not data["accumulated_issues"]:
            return ""

        feedback = "## Accumulated Minor/Trivial Issues\n\n"
        feedback += f"Total issues: {data['current_count']}\n\n"

        # Group by severity
        by_severity = {}
        for issue in data["accumulated_issues"]:
            severity = issue["severity"]
            if severity not in by_severity:
                by_severity[severity] = []
            by_severity[severity].append(issue)

        for severity in ["minor", "trivial"]:
            if severity in by_severity:
                feedback += f"### {severity.upper()} ({len(by_severity[severity])})\n\n"
                for issue in by_severity[severity]:
                    feedback += f"**{issue['file']}:{issue['line']}**\n"
                    feedback += f"- {issue['description']}\n"
                    feedback += f"- Suggestion: {issue['suggestion']}\n\n"

        return feedback

    def _clear_accumulated_fixes(self, pr_number: int) -> None:
        """Clear accumulated fixes after sending feedback."""
        fix_file = self.accumulated_fixes_dir / f"pr-{pr_number}.json"
        if fix_file.exists():
            fix_file.unlink()
```

**_try_review_pr() 改修:**

```python
def _try_review_pr(self, pr: PullRequest) -> bool:
    """Review a PR with severity-based handling."""

    # ... existing lock acquisition code ...

    try:
        # Get issue and diff
        issue = self.github.get_issue(issue_number)
        diff = self.github.get_pr_diff(pr.number)

        # Review with severity classification
        logger.info(f"[{self.agent_id}] Reviewing PR with severity classification...")
        review_result = self.llm.review_code_with_severity(
            spec=issue.body,
            diff=diff,
            repo_context=f"Repository: {self.repo}",
            work_dir=Path.cwd(),
        )

        if not review_result.success:
            raise RuntimeError(f"Review failed: {review_result.error}")

        # Parse review result (JSON)
        try:
            review_data = json.loads(review_result.output)
        except json.JSONDecodeError:
            raise RuntimeError("Failed to parse review result as JSON")

        overall_decision = review_data.get("overall_decision", "request_changes")
        issues = review_data.get("issues", [])
        summary = review_data.get("summary", "")

        # Separate issues by severity
        critical_major = [i for i in issues if i["severity"] in ["critical", "major"]]
        minor_trivial = [i for i in issues if i["severity"] in ["minor", "trivial"]]

        # Handle critical/major issues immediately
        if critical_major:
            logger.info(f"[{self.agent_id}] Found {len(critical_major)} critical/major issues, requesting changes")

            # Format feedback
            feedback = "## Critical/Major Issues\n\n"
            for issue in critical_major:
                feedback += f"**[{issue['severity'].upper()}] {issue['file']}:{issue['line']}**\n"
                feedback += f"- {issue['description']}\n"
                feedback += f"- Suggestion: {issue['suggestion']}\n\n"

            feedback += f"\n## Summary\n{summary}"

            # Request changes
            self.github.remove_pr_label(pr.number, self.STATUS_IN_REVIEW)
            self.github.add_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)
            self.github.request_changes_pr(pr.number, feedback)

            return True

        # Handle minor/trivial issues - accumulate
        if minor_trivial:
            logger.info(f"[{self.agent_id}] Found {len(minor_trivial)} minor/trivial issues, accumulating...")

            # Add to accumulated fixes
            threshold_reached = False
            for issue in minor_trivial:
                if self._add_accumulated_issue(pr.number, issue):
                    threshold_reached = True

            if threshold_reached:
                logger.info(f"[{self.agent_id}] Threshold reached, sending accumulated feedback")

                # Format and send accumulated feedback
                feedback = self._format_accumulated_feedback(pr.number)
                feedback += f"\n\n## Summary\n{summary}\n\n"
                feedback += "These are accumulated minor/trivial issues. Please address them when convenient."

                # Request changes
                self.github.remove_pr_label(pr.number, self.STATUS_IN_REVIEW)
                self.github.add_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)
                self.github.request_changes_pr(pr.number, feedback)

                # Clear accumulated fixes
                self._clear_accumulated_fixes(pr.number)

                return True
            else:
                # Still accumulating, approve for now with comment
                logger.info(f"[{self.agent_id}] Still accumulating ({len(minor_trivial)} issues added)")

                comment = f"✅ **Approved with {len(minor_trivial)} minor/trivial issues noted**\n\n"
                comment += f"Issues are being accumulated ({self._load_accumulated_fixes(pr.number)['current_count']}/{self.ACCUMULATED_THRESHOLD}). "
                comment += "You'll receive consolidated feedback when threshold is reached.\n\n"
                comment += f"Summary: {summary}"

                self.github.comment_pr(pr.number, comment)
                # Continue to approval below

        # No issues or only accumulated minor/trivial - approve
        logger.info(f"[{self.agent_id}] Approving PR")

        self.github.remove_pr_label(pr.number, self.STATUS_IN_REVIEW)
        self.github.add_pr_label(pr.number, self.STATUS_APPROVED)

        approve_body = f"{summary}\n\n✅ Code review passed!"
        self.github.approve_pr(pr.number, approve_body)

        return True

    except Exception as e:
        logger.error(f"[{self.agent_id}] Failed to review PR #{pr.number}: {e}")
        # ... existing error handling ...
```

### B-5: テスト要件

**ファイル:** `tests/test_reviewer.py` (新規作成または拡張)

**必須テスト:**

1. `test_severity_classification_critical_immediate_feedback`
   - CRITICALな問題を検出→即座にchanges-requested

2. `test_severity_classification_major_immediate_feedback`
   - MAJORな問題を検出→即座にchanges-requested

3. `test_severity_classification_minor_accumulate`
   - MINORな問題を検出→蓄積（threshold未満）→approve with comment

4. `test_severity_classification_trivial_accumulate`
   - TRIVIALな問題を検出→蓄積→approve with comment

5. `test_accumulated_threshold_reached`
   - 蓄積がthreshold到達→まとめてchanges-requested

6. `test_accumulated_fixes_storage`
   - JSONファイルへの保存・読み込みが正常動作

7. `test_mixed_severity_critical_wins`
   - CRITICAL+MINOR混在→CRITICALのみでchanges-requested（MINORは無視）

8. `test_no_issues_approve`
   - 問題なし→即座にapprove

---

## Phase C: Planner フィードバックループ

### 目的

Worker Agentが最大再試行後も実装に失敗した場合、Planner Agentに仕様の見直しを依頼する仕組みを実装する。

### 背景

現在、Workerが3回再試行しても実装に失敗した場合、単に`status:failed`を付与して停止する。これにより：
- 仕様が不明確な場合、ユーザーが手動で介入する必要がある
- 失敗原因が仕様にある場合、Plannerが再検討する機会がない

### 解決策

**Planner フィードバックループ**を導入し、Worker失敗時にPlannerに自動通知して仕様改善を促す。

---

## Phase C 詳細仕様

### C-1: Worker失敗時の新ラベル

```python
# worker-agent/main.py に追加

STATUS_NEEDS_CLARIFICATION = "status:needs-clarification"
```

### C-2: Worker失敗ハンドリング改修

**ファイル:** `worker-agent/main.py`

**_try_process_issue() の例外ハンドリング改修:**

```python
    except Exception as e:
        logger.error(
            f"[{self.agent_id}] Failed to process issue #{issue.number}: {e}"
        )

        # Analyze failure reason
        failure_reason = str(e)
        is_spec_unclear = self._is_specification_unclear(failure_reason, issue.body)

        if is_spec_unclear:
            # Request clarification from Planner
            logger.info(
                f"[{self.agent_id}] Specification unclear, requesting Planner clarification"
            )

            self.lock.mark_needs_clarification(
                issue.number,
                self.STATUS_IMPLEMENTING,
                failure_reason,
            )

            # Create detailed feedback for Planner
            feedback = self._generate_planner_feedback(
                issue_number=issue.number,
                spec=issue.body,
                failure_reason=failure_reason,
                attempt_count=test_retry_count + 1,
            )

            self.github.comment_issue(
                issue.number,
                f"⚠️ **Implementation failed - Specification clarification needed**\n\n"
                f"{feedback}\n\n"
                f"@Planner: Please review and clarify the specification.",
            )

            # Remove implementing label, add needs-clarification
            self.github.remove_label(issue.number, self.STATUS_IMPLEMENTING)
            self.github.add_label(issue.number, self.STATUS_NEEDS_CLARIFICATION)
        else:
            # Technical failure, mark as failed
            self.lock.mark_failed(
                issue.number,
                self.STATUS_IMPLEMENTING,
                failure_reason,
            )

        # Cleanup branch if it was created
        self.git.cleanup_branch(f"auto/issue-{issue.number}")

        return False
```

**新規メソッド追加:**

```python
def _is_specification_unclear(self, failure_reason: str, spec: str) -> bool:
    """
    Determine if failure is due to unclear specification.

    Args:
        failure_reason: The exception/error message
        spec: The issue specification

    Returns:
        True if specification clarification needed, False if technical failure
    """
    # Heuristics for spec-related failures
    spec_unclear_keywords = [
        "ambiguous",
        "unclear",
        "not specified",
        "undefined behavior",
        "missing requirement",
        "conflicting requirement",
        "cannot determine",
        "insufficient information",
    ]

    failure_lower = failure_reason.lower()

    # Check for spec-related keywords
    for keyword in spec_unclear_keywords:
        if keyword in failure_lower:
            return True

    # Check if spec is too short (less than 100 chars = likely unclear)
    if len(spec.strip()) < 100:
        return True

    # Check if multiple test failures with different reasons (suggests unclear spec)
    if "Test failed after" in failure_reason and "different" in failure_lower:
        return True

    return False

def _generate_planner_feedback(
    self,
    issue_number: int,
    spec: str,
    failure_reason: str,
    attempt_count: int,
) -> str:
    """
    Generate detailed feedback for Planner to improve specification.

    Args:
        issue_number: Issue number
        spec: Original specification
        failure_reason: Why implementation failed
        attempt_count: Number of attempts made

    Returns:
        Formatted feedback message for Planner
    """
    feedback = f"""## Implementation Failure Analysis

**Issue Number:** #{issue_number}
**Attempts Made:** {attempt_count}/{self.MAX_RETRIES}
**Agent ID:** {self.agent_id}

### Failure Reason
```
{failure_reason[:1000]}
```

### Original Specification
```
{spec[:1000]}
```

### Clarification Needed

Based on the failure analysis, the specification may need improvement in these areas:

1. **Acceptance Criteria**: Are the success conditions clearly defined?
2. **Edge Cases**: Are boundary conditions and error cases specified?
3. **Dependencies**: Are all required dependencies and prerequisites listed?
4. **Data Formats**: Are input/output formats clearly specified?
5. **Error Handling**: How should errors and exceptions be handled?

### Recommendations for Planner

Please review the specification and:
- Add missing acceptance criteria
- Clarify ambiguous requirements
- Specify edge case handling
- Add concrete examples
- Break down complex requirements into smaller steps

Once clarified, please update the issue and change label from `status:needs-clarification` back to `status:ready`.
"""

    return feedback
```

### C-3: LockManager拡張

**ファイル:** `shared/lock.py`

```python
def mark_needs_clarification(
    self,
    issue_number: int,
    current_status: str,
    reason: str,
) -> None:
    """
    Mark an issue as needing clarification.

    Similar to mark_failed but signals Planner intervention needed.
    """
    logger.warning(
        f"Issue #{issue_number} needs clarification: {reason}"
    )

    # Note: Label transition is handled by Worker
    # This is just for logging and potential future expansion
```

### C-4: Planner Agent通知受信（オプション）

**ファイル:** `planner-agent/main.py` (将来の拡張)

現時点では、Plannerは手動で`status:needs-clarification`ラベルのIssueを確認する。

**将来の自動化案:**
```python
def _process_clarification_requests(self) -> None:
    """Find and process issues needing clarification."""
    issues = self.github.list_issues(labels=["status:needs-clarification"])

    for issue in issues:
        logger.info(f"Issue #{issue.number} needs clarification")

        # Extract Worker feedback from comments
        comments = self.github.get_issue_comments(issue.number)
        worker_feedback = self._extract_worker_feedback(comments)

        # Present to user or LLM for clarification
        # ... (future implementation)
```

### C-5: テスト要件

**ファイル:** `tests/test_worker.py`

**必須テスト:**

1. `test_specification_unclear_detection`
   - 不明瞭な仕様を検出→`status:needs-clarification`

2. `test_technical_failure_still_marked_failed`
   - 技術的失敗は従来通り`status:failed`

3. `test_planner_feedback_generation`
   - Planner向けフィードバックが適切に生成される

4. `test_clarification_comment_posted`
   - Issueに明確化依頼コメントが投稿される

5. `test_short_spec_triggers_clarification`
   - 短すぎる仕様（<100文字）で明確化要求

---

## 実装順序推奨

### Phase B実装ステップ

1. **B-Step 1:** `shared/llm_client.py` に `review_code_with_severity()` 追加
2. **B-Step 2:** `reviewer-agent/main.py` に重大度分類enum、蓄積ストレージ追加
3. **B-Step 3:** `_try_review_pr()` を重大度ベースロジックに改修
4. **B-Step 4:** テスト作成・実行
5. **B-Step 5:** ドキュメント更新（CLAUDE.md, README.md）

### Phase C実装ステップ

1. **C-Step 1:** `STATUS_NEEDS_CLARIFICATION` 定数追加
2. **C-Step 2:** `_is_specification_unclear()` メソッド実装
3. **C-Step 3:** `_generate_planner_feedback()` メソッド実装
4. **C-Step 4:** 例外ハンドリングを改修してPlanner通知追加
5. **C-Step 5:** テスト作成・実行
6. **C-Step 6:** ドキュメント更新

---

## 設定値

### Phase B設定

```python
# reviewer-agent/main.py
ACCUMULATED_THRESHOLD = 5  # 蓄積5件でまとめて返す
ACCUMULATED_FIXES_DIR = "~/.workflow-engine/accumulated_fixes/"
```

### Phase C設定

```python
# worker-agent/main.py
MIN_SPEC_LENGTH = 100  # 最低仕様文字数
SPEC_UNCLEAR_KEYWORDS = [
    "ambiguous", "unclear", "not specified",
    "undefined behavior", "missing requirement",
]
```

---

## 期待される成果物

### Phase B

- [ ] `shared/llm_client.py` に `review_code_with_severity()` 追加
- [ ] `reviewer-agent/main.py` に重大度分類・蓄積機能追加
- [ ] 蓄積ストレージの実装（JSON）
- [ ] 8つ以上のテスト（全パス）
- [ ] CLAUDE.md, README.md更新

### Phase C

- [ ] `worker-agent/main.py` に仕様明確化判定ロジック追加
- [ ] Planner向けフィードバック生成機能
- [ ] `status:needs-clarification` ラベル導入
- [ ] 5つ以上のテスト（全パス）
- [ ] CLAUDE.md, README.md更新

---

## 検証方法

### Phase B検証

1. **重大度分類テスト:**
   ```bash
   uv run pytest tests/test_reviewer.py::test_severity_classification_* -v
   ```

2. **蓄積機能テスト:**
   ```bash
   uv run pytest tests/test_reviewer.py::test_accumulated_* -v
   ```

3. **E2Eテスト:**
   - 手動でCRITICAL問題を含むPR作成→即座にchanges-requested確認
   - MINOR問題5件蓄積→まとめてchanges-requested確認

### Phase C検証

1. **仕様明確化検出テスト:**
   ```bash
   uv run pytest tests/test_worker.py::test_specification_unclear_* -v
   ```

2. **E2Eテスト:**
   - 不明瞭な仕様（<100文字）でIssue作成→Worker失敗後に`status:needs-clarification`確認
   - Issueコメントに詳細なPlanner向けフィードバック確認

---

## 参考情報

### 既存コードベースパターン

**LLMClient呼び出しパターン:**
```python
result = self.llm.generate_implementation(
    spec=spec,
    repo_context=f"Repository: {self.repo}",
    work_dir=self.git.path,
)

if not result.success:
    raise RuntimeError(f"Generation failed: {result.error}")
```

**GitHub操作パターン:**
```python
# ラベル操作
self.github.add_label(issue.number, STATUS_LABEL)
self.github.remove_label(issue.number, OLD_STATUS)

# コメント投稿
self.github.comment_issue(issue.number, message)
self.github.comment_pr(pr.number, message)

# PRレビュー
self.github.approve_pr(pr.number, body)
self.github.request_changes_pr(pr.number, body)
```

**テストパターン:**
```python
@patch("shared.github_client.GitHubClient")
@patch("shared.llm_client.LLMClient")
def test_something(self, mock_llm, mock_github):
    agent = ReviewerAgent("owner/repo")
    agent.llm = mock_llm.return_value
    agent.github = mock_github.return_value

    # モック設定
    agent.llm.review_code.return_value = LLMResult(
        success=True,
        output="review result"
    )

    # テスト実行
    result = agent._try_review_pr(pr)

    # アサーション
    assert result is True
    agent.github.approve_pr.assert_called_once()
```

---

## コミットメッセージ規約

Phase Aのパターンに従う:

```
feat: add severity-based review (Phase B Step 1)

- Add review_code_with_severity() to LLMClient
- Classify issues as critical/major/minor/trivial
- Return structured JSON with severity info

This enables Reviewer to handle issues based on severity.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>
```

---

## 注意事項

1. **既存機能を壊さない**: Phase A（CI自動修正）は完全に動作し続けること
2. **後方互換性**: 既存のラベル体系、ワークフローとの互換性を保つ
3. **テストカバレッジ**: 新機能は必ず包括的なテストでカバーする
4. **ドキュメント**: CLAUDE.md と README.md は必ず更新する
5. **型ヒント**: 全ての新規関数に型ヒントを追加する（mypy通過）
6. **pre-commit**: 全てのコミット前にpre-commitフックが通過すること

---

## 質問・不明点

実装中に不明点があれば、以下を参照:
- Phase Aの実装パターン（`worker-agent/main.py`の`_wait_for_ci()`等）
- 既存テスト（`tests/test_worker.py`, `tests/test_github_client.py`）
- CLAUDE.md の既存パターン説明

---

**作成日:** 2025-01-31
**Phase A完了コミット:** 4b75597
**対象バージョン:** workflow-engine v1.0
