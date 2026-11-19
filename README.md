# cc-schema-lab

CC_Codex 二期（26.11）**数据工程**产品线：**契约换版台**（aiohttp + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**表契约版本登记 → 破坏性检查 → 换版应用 → 目录对账**

与现有九线正交（不是采集、ETL、同步、解析、编排、摄取、留存、清单、水印）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（表契约 / 换版闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、aiohttp、SQLite |
| 前端 | 原生 JS SPA（全中文），暖杏纸面 + 墨绿强调；顶栏胶囊导航 |
| 测试 | pytest + pytest-aiohttp（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 表契约版本（表名 / 版本号 / 现行或草稿）
2. 对候选字段做破坏性检查（删列 / 改类型）
3. 换版应用（草稿升现行，旧现行停用）
4. 契约与目录表对账

## 脱敏出站（本批加固）

敏感列（契约 `contract_fields.sensitive`）经页面脱敏标记 + 字段笺 → 出站规则 → 合规水线四处联查。

- **判定只钉一边：提交时重算**。“刷新脱敏”提交那一刻按契约+页面标记重算出站规则并落库；
  敏感列在提交瞬间若仍会明文出站，整次刷新拒绝，一条规则都不落。
- 页面脱敏与契约脱敏必须核齐；契约标敏感而页面关着 = 假明文，刷新即拒。
- 出站卡片明文即拒；历史明文在刷新时收回成 `***` 并打隔离标。
- 两名合规员同时刷新有锁（进程内 + 库内 TTL 锁），不会双写规则。
- 脱敏列表亮“已重算 / 已过期·待刷新”芯片与快照摘要；合规水线读同一份标记，
  对账报告落 `compliance_ok` / `compliance_detail` / `compliance_digest`。
- 复现与本地库核对见 [`复现纸.md`](复现纸.md)。

预留切口（未做，供后续功能题）：双写迁移 vs 锁表切流、换版失败闭、并发应用互斥、跨表依赖冻结、回滚窗口等。

## 本地

```powershell
cd claude-workspace\cc-schema-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7100/login · **admin / 123456**

## Docker

端口 **7100**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
