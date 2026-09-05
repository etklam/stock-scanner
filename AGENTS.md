# AGENTS.md — 給 coding agent 嘅項目規則

## CI／CD：唔好用 GitHub Actions（收費）

**規則：** 唔好建立、修改或重新加入任何 GitHub Actions workflow（`.github/`）。
GitHub-hosted runners 對呢個 repository 產生費用（macOS runner 尤其貴）。
2026-09-06 起 `.github/workflows/ci.yml` 已移除，唔好因為「慣性」或 template
而加返嚟——包括看似免費嘅試用、self-hosted 以外嘅任何 hosted runner。

**點做品質把關（代替 CI）：**

```sh
uv run python scripts/check.py          # 全套：ruff/mypy/pytest/OpenAPI/build/wheel smoke
uv run python scripts/check.py --fast   # 快版：唔 build、唔裝 wheel
```

- 提交前至少行 `--fast`；release 前行全套，並喺**每個支援平台手動行一次**
  （單機代替唔到跨平台驗證，文件宣稱要如實）。
- 文件／報告入面提及「CI」一律指歷史紀錄；現行驗證 = 本地 `scripts/check.py`。

## SQLite runtime：release 前必須核實（5.1 新規則）

- Python 內建 SQLite 有官方 WAL-reset bug advisory（sqlite.org/wal.html
  #walresetbug；修復 3.51.3，2026-03-13；backport 3.50.7／3.44.6）。
- **release 驗收前必須確認實際 runtime 的 `sqlite3.sqlite_version`** 屬
  3.51.3+ 或上述 backport；`qscan doctor` 的 `sqlite_runtime` 檢查會列出判定
  （`sqlite_wal_reset_status`；同分支非 backport 的較新 patch 亦報 AFFECTED）。
- pip/OSV 套件 audit 不等於內建 SQLite runtime audit；「低機率」不得寫成
  「無已知問題」。已知 blocker 記錄於 docs/release-checklist.md。

## 其他既有規則（摘要，詳見 docs/development-plan.md 第 16 節）

- Python 3.12 + uv；唔好隨便升 dependency 或改 `uv.lock`；只為實際驗收失敗做必要修正。
- 分層不動：core 無 I/O；CLI 與 HTTP API 共用 application services，唔副本業務邏輯。
- 一個本機 SQLite、一個 serial executor；唔引入 Redis/PostgreSQL/微服務。
- JSON stdout 只有一份 JSON；診斷行 stderr；token 唔好出現喺任何輸出或 log。
- Regression tests 要能令舊行為失敗；唔好用 blanket skip 或放寬 assertion 換綠燈。
- Backup/還原語義見 docs/adr/0006-backup-format.md；唔好簡化到只 copy 主 DB 檔。
