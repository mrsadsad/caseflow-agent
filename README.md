# CaseFlow · 可恢复、可审批的售后执行 Agent

CaseFlow 接到退款诉求后，查询订单、检索当前租户政策，选择补问、转人工或提出退款方案。**模型只能提出方案；人工批准具体金额与版本后，执行器才向本地退款模拟器记账。**

这个项目着重回答一个工程问题：模型输出不稳定、人员审批有延迟、执行进程会中断，怎样仍然让业务动作可追溯、可恢复，并避免重复退款？它补充了已有 OpsLens 的故障调查和 PatchForge 的代码修复方向。

Python 3.11+，核心依赖只有标准库。不需要 Docker、外部数据库或模型 Key 就能演示完整闭环。当前为单机 0.1 工程原型，没有连接真实支付系统。

![CaseFlow 本地工作台](docs/workbench.png)

## 立即运行

克隆仓库后执行（Python 3.11+）：

```powershell
git clone https://github.com/mrsadsad/caseflow-agent.git
cd caseflow-agent
python -m unittest discover -s tests -v
python -m caseflow serve --port 8780
```

打开 [http://127.0.0.1:8780](http://127.0.0.1:8780)。也可以运行 `./start.ps1`。

页面默认选择固定脚本演示。前台服务按 Ctrl+C 退出。`stop.ps1` 仅用于停止存在 `runs/server.pid` 记录的本地后台服务。

1. 选择可退款案例，以「固定脚本演示」开始，查看工具轨迹和政策引用。
2. 检查提案金额、订单版本和政策版本，批准方案，此时产生待投递任务。
3. 点击故障演练：**台账已写入，Outbox 尚未确认**。
4. 再次投递：台账仍然只有一笔，任务变为完成。
5. 运行跨租户、跨客户或超期案例，查看拒绝与转人工。

演示会改变数据库中的订单状态。若要重新开始，指定一个新的数据库文件，保留已有实验记录：

```powershell
python -m caseflow serve --port 8781 --db runs/fresh-demo.sqlite3
```

服务仅监听本机。一个数据库只应由一个活动服务/CLI 执行进程使用；服务运行期间，额外 CLI 演示使用其他 `--db`。

## 可以深入讲解的实现

| 能力 | 实际实现 | 面试中要解释的问题 |
| --- | --- | --- |
| 动态工具调用 | 原生 function calling，依据工具结果继续决策 | 哪些步骤需要模型，哪些适合确定性代码？ |
| 范围受限的检索 | 先按租户、有效版本过滤，再按词项匹配排序 | 为什么不能先全量召回，再让模型忽略无权数据？ |
| 证据与业务门控 | 引用必须来自本次检索，金额用整数分，资格由规则验证 | 引用存在为什么不等于语义正确？ |
| 具体提案审批 | 审批与执行前都重新检查订单和政策版本 | 调查完成后订单状态变了怎么办？ |
| 事务 Outbox | 批准与待投递任务在同一事务写入 | 如何避免批准成功却丢失执行任务？ |
| 幂等接收端 | 唯一动作键、订单可退额度与台账原子更新 | 重投同一动作与两个不同提案有哪些区别？ |
| 持久化模型循环 | 请求前预留预算，保存响应与待执行工具 | 请求结果未知后，如何继续且不重置预算？ |
| 两种评测策略 | 固定预取后单次决策 vs 动态工具循环 | 更多模型调用是否真的带来更好结果？ |

框架选择以控制点是否清楚为准。当前采用显式循环；持久化和中断也可以交给 LangGraph 等框架，但业务接收端的幂等仍需自行设计。[LangGraph 持久化文档](https://docs.langchain.com/oss/python/langgraph/persistence)、[中断文档](https://docs.langchain.com/oss/python/langgraph/interrupts)。

## 使用真实模型

默认模型为 `deepseek-flash`，采用非思考模式与工具调用。[DeepSeek 调用协议](https://api-docs.deepseek.com/api/create-chat-completion/)、[工具调用文档](https://api-docs.deepseek.com/guides/tool_calls/)。其他兼容服务可配置 `CASEFLOW_BASE_URL`、`CASEFLOW_MODEL`、`CASEFLOW_API_KEY`，兼容性需各自实测。

将本项目 `.env.example` 复制为 `.env`，在本机填写模型 Key：

```powershell
Copy-Item .env.example .env
# 编辑 .env 后执行；macOS/Linux 可用 cp .env.example .env 复制。
python -m caseflow doctor
python -m caseflow serve
```

在工作台选择「真实模型」会消耗所配置 API 的额度。Key 仅由服务端读取，不进入页面、事件或评测快照。运行命令：

```powershell
python -m caseflow run --case refund_valid --mode live --db runs/live.sqlite3
```

真实模式依然只操作本地合成业务数据，不会发生真实退款。固定脚本模式不是模型自主能力证明。若已有其他位置的配置文件，可用 `--env-file <配置文件路径>` 显式选择。

## 评测与结果留存

```powershell
# 无模型调用：9 类案例 × 2 种策略，验证软件闭环
python -m caseflow evaluate --mode demo

# 小规模真实对照：3 类案例 × 两策略，最坏 27 次模型请求
python -m caseflow evaluate --mode live --cases refund_valid prompt_injection foreign_order --strategy both --max-steps 8 --max-model-calls 27

# 完整开发集：9 × (1+8)，最坏 81 次请求
python -m caseflow evaluate --mode live --strategy both --max-model-calls 81
```

`direct` 用固定工具预取订单和政策，然后仅一次模型决策；`agent` 自行选择查询，最多八次请求。业务校验和最终审批执行规则一致，但模型调用预算与上下文获取方式不同。这里比较的是策略系统，不能解释成完全相同预算下的纯模型能力差异。

每个尝试使用独立数据库。评分检查预期决策、审批前无副作用、批准金额与归属，以及故障重放后仅一笔台账。自动批准仅用于隔离评测模拟器，工作台仍需人工批准。失败和预算耗尽保留在分母，未知用量不会记成完整的零成本。

产物保存在 `runs/evaluation-*/`：配置、源码 SHA-256 与源码快照、逐案例消息和事件、台账、评分和汇总。评测案例全部是人工构造的开发数据，政策文本较短，不是独立盲测或生产泛化证据。参考的评测方法强调观察执行结果与完整轨迹，而不能只看 Agent 的最终回答。[Anthropic Agent 评测说明](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)。

实际完成的测试与实验记录见 [VALIDATION.md](docs/VALIDATION.md)。

## CLI 与恢复

```powershell
python -m caseflow run --case refund_valid --db runs/cli.sqlite3
python -m caseflow list --db runs/cli.sqlite3
python -m caseflow approve <proposal_id> --db runs/cli.sqlite3
python -m caseflow dispatch --crash-after-effect --db runs/cli.sqlite3
python -m caseflow dispatch --db runs/cli.sqlite3
python -m caseflow resume <run_id> --db runs/cli.sqlite3
```

`resume` 适用于中断或请求错误的模型运行；已完成业务提案、执行失败和预算耗尽不能通过恢复获得新授权或新预算。启动服务会将遗留的 `ready`（已入队）与 `running` 标记为 `interrupted`，不会自动发起收费模型调用。

请求次数是硬限制。每次模型输出最多 1200 token，累计上下文有字符限制；这些不是金额硬预算。HTTP 错误不做隐式重试，API 请求未知结果可能已计费。

## 目录与边界

```text
caseflow/
  runtime.py      # 模型循环、预算、响应重放
  provider.py     # 模型协议与配置
  tools.py        # 权限范围、工具参数、证据门控
  store.py        # 事务、版本校验、Outbox、模拟退款
  fixtures.py     # 9 类可复现合成案例
  evaluation.py  # 两策略与逐案例结果
  server.py      # 本机 HTTP 服务
  web/index.html # 工作台
tests/           # 业务冲突、崩溃、协议与服务测试
docs/            # 设计、验证与面试说明
```

- 只支持售后申请的有限业务动作；未实现真实支付接入、身份认证、多节点任务调度或公网部署。
- `needs_input` 表示需要补充资料；本版尚无用户补充信息后继续同一会话的入口。`escalated` 是本地终态，不会发送真实工单。
- 词项检索没有 embedding 或 reranker；来源验证不保证解释语义正确。提示注入测试不等于模型对所有攻击免疫。
- 订单在首次播种时生成 UTC 创建时间，审批和执行都会重新计算订单年龄；没有政策管理后台或生产数据接入。
- 本地幂等效果不能替代真实网关的幂等协议、状态查询和对账。

阅读：[设计说明](docs/ARCHITECTURE.md) · [面试讲解与个人练习](docs/INTERVIEW.md)。能独立解释、改造和验证这些机制，才是你可以带进面试的能力证据；项目名称或测试数量不能保证录用。
