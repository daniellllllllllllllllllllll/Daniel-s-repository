# 相关错误感知的可靠调度与验证 —— 运行手册

> 配套：`PROPOSAL_CORRELATION.md`（方案）、`results_correlation/`（已产出的实验数据）、
> `PAPER_DRAFT.md`（论文草稿）。

## 1. 环境（本机已就位）

```powershell
# 依赖已装在 venv_clean；如需重建：
python -m venv venv_clean
venv_clean\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item BlkMac_Offline_Files\.env .env      # 只复制，不要读内容
```

### 沙箱/本机两个已知坑

1. **`pip` 在受限环境里会因临时目录权限失败**（`OSError: Check the permissions`，根因是
   `tempfile.mkdtemp` 以 `0o700` 建目录后被拒绝写入）。绕过脚本：`tools\pip_patched.py`
   （把 `os.mkdir` 的 `0o700` 放宽为 `0o777`）。
2. **离线 `.env` 里的 `LLM_PROVIDER` 不是本代码支持的后端名**，直接跑会抛
   `LLMConfigError`。`config.py` 用 `load_dotenv(override=False)`，**真实环境变量优先**，
   所以两条路都可以（都不需要改 `.env`）：
   - 命令行加 `--provider mock`；
   - 或测试时用 `tools\run_tests.py`（自动注入 `LLM_PROVIDER=mock` 并打上临时目录权限补丁）。

## 2. 跑单测

```powershell
venv_clean\Scripts\python.exe tools\run_tests.py          # 期望: Ran 304 tests ... OK (skipped=9)
```

跳过的是链上用例（需要 Ganache 私链，未启动时自动 skip，属预期）。

## 3. 回归护栏（必须逐位一致）

```powershell
venv_clean\Scripts\python.exe -m controller.main_controller --mode all --provider mock `
  --no-chain --no-plot --agents 4 --seed 20250728 --out _regression_check
```

与 `BlkMac_Offline_Files\results_ganache\metrics.csv` 对比，以下字段必须相同：
`questions / correct / accuracy / hallucinations / total_subtasks / load_variance`
（与 `BlkMac_Offline_Files/results_ganache` 逐位一致；具体数值本地保留，不随仓库发布）。
时间类字段与链上字段本机不可比。

## 4. 跑实验矩阵（P5）

```powershell
venv_clean\Scripts\python.exe -m controller.correlation_experiment `
  --agents 4 --seed 20250728 --hallucination-rate 0.5 --out results_correlation
```

产出：`metrics.csv`、`per_question.csv`、`manipulation_check.csv`、`run_config.json`、
`fig_accuracy_budget.png`、`fig_error_acceptance.png`、`CONCLUSIONS.md`（自动给出 H1/H2/H3 判定）。

**为什么必须调 `--hallucination-rate`**：默认设置下 C 组在 24 题上已是 100% 正确率、错误接受率 0，
冗余验证无从体现收益，矩阵会退化成全 0。这是压力校准，论文里必须如实说明。

## 5. 真实模型交叉验证（P6）—— **需要你手动执行**

脚本已就绪，但**当前执行环境连不上配置里的 LLM 端点**
（`urllib3` 超时 `WinError 10060`，3 次重试均失败），所以这一步没有产出数据。

```powershell
# 用 .env 里的 LLM_API_KEY / LLM_BASE_URL，只覆盖 provider 名
venv_clean\Scripts\python.exe -m controller.main_controller --mode c --provider openai `
  --no-chain --no-plot --limit 8 --out results_correlation_real
```

要点：
- `--out results_correlation_real` 是**新目录**，不会覆盖任何既有结果；
- 真实模型与 Mock 结果**不可混算/混图**（既有 `results_real_gpt54_corrected` 已证明两者差异巨大）；
- 真实模型下方差大，`--limit 8` 只够冒烟，正式结论需要多次重复并报告置信区间；
- 若你的端点需要特定 provider 名，`--provider` 只接受 `openai` / `anthropic` / `mock`。

## 6. 关键参数（`.env.example` 已收录，均为默认关闭）

| 参数 | 默认 | 含义 |
|---|---|---|
| `REDUNDANCY_BUDGET` | 0 | 每题允许的额外 LLM 调用数；0 = 与既有 C 组完全一致 |
| `SCORE_W_CORRELATION` | 0.0 | 相关性修正项权重（消融开关，当前未接入打分函数） |
| `CORRELATION_MIN_SAMPLES` | 4 | 冷启动（能力余弦先验）→ 热启动（Pearson）的样本阈值 |
| `BETA_PRIOR_ALPHA` / `BETA_PRIOR_BETA` | 1.0 / 1.0 | per-domain Beta 先验（均匀） |

CLI 侧：`--method {baseline,random,reputation,proposed}`、`--redundancy-budget N`、
`--correlation-level {none,independent,pairs,all}`。

## 7. 已知局限（写论文/汇报时必须带上）

1. **H1 不成立**：高相关场景下 Proposed 的错误接受率并不优于强基线 B2；成立的是 H2（成本更低）与 H3（不更差）。
2. 独立错误档下基线 B0 略优于所有冗余方法（平票时保留主 Agent 答案）——已如实记录。
3. MockLLM 是受控模拟，非真实模型行为；难度经过 `--hallucination-rate` 压力校准。
4. RSA/TCP 加密未进入量化对比；主实验 C 组用 `transport=direct`。
5. Gas 在不同历史结果目录中存在两个不同值，canonical 值待复核，本方案不涉及。
