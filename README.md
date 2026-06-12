# XRP-USDT Price Action Tracker

基于价格行为的 XRP-USDT 定时跟踪分析系统，每15分钟自动执行，通过 **ima OpenAPI 搜索知识库 + PyPDF2 解析PDF** 将交易规则融入分析逻辑，通过 **ServerChan** 推送分析结果。

## 分析逻辑

采用**简单回调交易系统**：
1. **1H时间框架**：确认趋势方向（摆动结构 HH/HL/LH/LL + SMA20/50）
2. **15M时间框架**：寻找趋势回调结束点
3. **知识库增强**：ima OpenAPI 搜索"Space的知识库"→ 下载PDF → 提取规则 → 融入分析
4. **交易计划**：顺1H趋势方向，在15M回调结束确认后入场

## 部署步骤

### 1. 创建 GitHub 仓库（推荐公开仓库，免费额度无限）

```bash
# GitHub 上新建仓库，名称如 xrp-tracker
# Public（公开）→ 免费 Action 分钟数无限
# Private（私有）→ 每月2000分钟，每15分钟会超限
```

### 2. 设置 GitHub Secrets

仓库 → Settings → Secrets and variables → Actions → **New repository secret**

| Secret 名称 | 值 |
|:-----------|:----|
| `SC_SENDKEY` | `sctp21263t6jbuqmbkuubvg83m1cnpat` |
| `IMA_CLIENT_ID` | `817ed8cb6ad95e60b1820e9134e2ab6f` |
| `IMA_API_KEY` | `yRcNbFqX496RQu6rf3D7qaNFFWeGXCHnmRyoQJvHeI5Qnq2vJ3ftfjNBSy/JtBJCB5bK4WYBRA==` |
| `IMA_KB_ID` | `OaY9MZEm7Mp4evh57u9yOZt3AmbdjChC35YQDSgaZtY=` |

### 3. 推送代码到 GitHub

```bash
cd /path/to/xrp-tracker-github
git init
git add .
git commit -m "Init: XRP-USDT price action tracker (知识库增强版)"
git remote add origin https://github.com/<你的用户名>/xrp-tracker.git
git push -u origin main
```

### 4. 手动测试

仓库 → Actions → XRP-USDT Price Action Tracker → **Run workflow**

检查 ServerChan 是否收到"定时分析总结"推送。

## 注意事项

| 项目 | 说明 |
|:-----|:-----|
| **公开仓库** | 免费 Action 分钟数无限，但 Secrets 不会暴露 |
| **私有仓库** | 每月2000分钟，每15分钟 = 约2880分钟/月 → **会超限**，建议改为 `/30 * * * *` 或公开仓库 |
| **cron 延迟** | GitHub Actions 的 cron 最多有5-10分钟延迟 |
| **数据源** | CryptoCompare API（免费），无需额外 Key |
| **知识库** | 每次运行通过 ima OpenAPI 实时搜索并解析知识库PDF |
