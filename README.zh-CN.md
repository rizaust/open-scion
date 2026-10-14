<p align="center"><a href="README.md">English</a> · <strong>简体中文</strong></p>

<p align="center"><img src="docs/assets/cover.svg" alt="Open Scion — 面向 agent 时代的版本控制" width="100%"></p>

<p align="center">
  <a href="https://rizaust.github.io/open-scion/zh.html">项目网站</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="docs/ARCHITECTURE.md">架构设计</a> ·
  <a href="docs/API.md">HTTP API</a> ·
  <a href="CONTRIBUTING.md">参与贡献</a>
</p>

<p align="center">
  <a href="https://github.com/rizaust/open-scion/actions/workflows/ci.yml"><img src="https://github.com/rizaust/open-scion/actions/workflows/ci.yml/badge.svg" alt="CI 状态"></a>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB" alt="Python 3.11 或更新版本">
  <img src="https://img.shields.io/badge/runtime_dependencies-0-234D36" alt="零第三方运行时依赖">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-234D36" alt="Apache 2.0 许可证"></a>
</p>

# Open Scion

**Open Scion 是对 Anthropic Scion 已公布核心架构的独立实现。**

这是一个面向开发者、编程 agent 和 CI 的服务端版本控制工具。服务端保存不可变 changeset 并执行仓库操作；工作区只保存你选择的文件和一个容量有限的 blob 缓存。创建 changeset 与推进共享分支是两个独立动作。

项目基于 Anthropic 于 2026 年 10 月 13 日发布的技术文章 [Redesigning source control at Anthropic](https://www.anthropic.com/engineering/redesigning-source-control) 中的架构思路独立开发，拥有自己的代码实现与 HTTP 协议。

**Python 3.11+ · 零第三方运行时依赖 · SQLite WAL · 内容寻址存储**

## 为什么选择 Open Scion？

- **工作区保持精简。** 只落盘选定的文件或目录。稀疏工作区提交时，未检出的路径仍然保留。
- **共享修改与推进分支分开。** `commit` 在服务端创建 changeset；`land` 再通过比较并交换保护推进 ref。
- **直接向服务端提问。** diff、历史、合并、rebase、blame 和搜索均提供 HTTP 接口。CI 可以不创建工作副本，直接调用 API。
- **复用不可变内容。** 使用 SHA-256 标识去重 blob 和持久化树节点。客户端下载后校验内容，并维护容量有限的本地缓存。
- **恢复与审计变更。** 工作副本日志支持中断恢复，仓库快照包含经过校验的对象，共享 ref 更新拥有事务级审计记录。
- **实现便于阅读。** 运行时仅使用 Python 标准库。只有可选的历史导入功能需要 Git。

## 快速开始

使用 Python 3.11 或更新版本从源码安装：

```sh
git clone https://github.com/rizaust/open-scion.git
cd open-scion
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

以下示例使用 POSIX shell。Windows 用户可在 PowerShell 中运行 `.venv\Scripts\Activate.ps1` 激活环境。CI 已配置 Linux、macOS、Windows 与 Python 3.11、3.13 的测试矩阵；实际验证状态请查看[当前工作流结果](https://github.com/rizaust/open-scion/actions/workflows/ci.yml)。

在第一个终端中启动仓库服务，并保持运行：

```sh
scion serve --data ../open-scion-data
```

在第二个终端中进入克隆的 `open-scion` 目录，激活同一环境，然后创建第一个工作区：

```sh
source .venv/bin/activate
scion init ../open-scion-work --server http://127.0.0.1:7433
cd ../open-scion-work

mkdir -p src docs
printf 'print("Hello, Scion")\n' > src/hello.py
printf '# Project guide\n' > docs/guide.md

scion status
scion diff
scion commit -m "Add a greeting and project guide" --author Ada
scion land --ref main
scion log
```

`commit` 上传发生变化的内容，在服务端创建不可变 changeset，并更新当前工作区版本，但不会改变 `main`。`land` 校验 ref 的预期值后推进 `main`；如果历史已经分叉，服务端会尝试合并。无需额外执行 push。

初始化要求目标目录为空或尚不存在。重复示例时请使用新目录。也可以不创建工作区，直接查询服务端：

```sh
curl http://127.0.0.1:7433/health
curl http://127.0.0.1:7433/v1/refs
```

## 完整工作流

<img src="docs/assets/workflow.svg" alt="实际运行的原生工作流：创建修改、推进分支并查询服务端历史" width="100%">

在克隆的仓库目录中激活环境，运行[独立演示](examples/demo.py)：

```sh
python examples/demo.py
```

演示会启动真实的本地 HTTP 服务，创建两个稀疏 agent 工作区，分别提交修改、完成两次 landing、检查仓库完整性，并自动清理临时文件。上面的命令流程也由子进程集成测试覆盖。

所有命令都支持 `--json`，可放在子命令前或后。成功结果输出到 stdout；结构化错误输出到 stderr，并返回非零退出码。

```sh
scion status --json
scion log --limit 5 --json
scion search "Hello" --rev main --json
```

## 只处理一个目录

完成快速开始后，再创建一个仅包含 `src` 的工作区：

```sh
scion init ../open-scion-src --server http://127.0.0.1:7433 --sparse src
cd ../open-scion-src

printf 'print("Hello from a sparse workspace")\n' > src/hello.py
scion commit -m "Update the greeting" --author Ada
scion land
scion search "Project guide" --prefix docs
```

`docs/guide.md` 虽然从未出现在这个工作区中，仍保留在仓库里，也可以在服务端被搜索到。可以同时选择多个路径前缀：`--sparse src docs`。

创建 `.scionignore`，排除未跟踪的构建产物或本地文件：

```gitignore
__pycache__/
*.pyc
/build/
.env
```

规则使用 shell 风格匹配。不带 `/` 的文件名模式可匹配任意层级；包含 `/` 的模式相对于工作区根目录。开头的 `/` 将规则锚定到根目录，结尾的 `/` 排除匹配的子树。空行和 `#` 注释会被忽略。不支持 `!` 反向规则，遇到时会明确报错。已跟踪的路径不受忽略规则影响；`.git` 和 `.scion` 始终排除。

## 架构

<img src="docs/assets/architecture.svg" alt="轻量工作区与 API 调用方连接同一个 HTTP 服务，服务端使用 SQLite 元数据和不可变压缩对象" width="100%">

Changeset 保存父版本、相对于第一父版本的文件增量、根树 ID 和元数据。更新持久化 Merkle 树时，仅复制受到影响的目录节点，并复用不变的子树。Ref 是单独保存的可变指针，与不可变历史分离。

客户端通过 HTTP 下载选定文件，并校验带类型信息的 SHA-256 标识。本地 blob 缓存上限为 64 MiB；清理缓存不会删除历史，因为服务端始终保存权威数据。工作区无需复制仓库历史数据库。

[架构文档](docs/ARCHITECTURE.md) 详细解释了存储布局、并发、合并语义、工作副本保护和实现取舍。[HTTP API 文档](docs/API.md) 说明了无需检出文件的读写方式。

### 源码结构

```text
open_scion/
├── cli/              # Parsing, command handlers, and output
│   └── commands/
├── client/           # HTTP transport and verified object API
├── graph/            # Generation-pruned traversal and metadata cache
├── importers/        # Git history adapters
├── operations/       # Inspection, text merging, and integration
├── server/           # Routes, validation, admission control, and telemetry
├── store/            # Objects, trees, changesets, refs, integrity, and snapshots
└── workspace/        # Scanning, cache, process locks, journals, and recovery
```

每个子包通过简洁的门面提供公共接口，内部模块分别负责持久化、协议和文件系统操作。

## 命令

运行 `scion COMMAND --help` 查看全部参数。工作区命令会从当前目录向父目录查找工作区。

| 任务 | 命令 |
| --- | --- |
| 启动仓库服务 | `scion serve --data DIRECTORY [--host HOST] [--port PORT]` |
| 创建工作区 | `scion init DIRECTORY --server URL [--rev REV] [--sparse PREFIX ...]` |
| 查看本地修改 | `scion status` · `scion diff` |
| 比较服务端版本 | `scion diff BASE TARGET [--prefix PATH]` |
| 创建 changeset | `scion commit -m MESSAGE [--author NAME]` |
| 推进共享 ref | `scion land [REV] [--ref main] [--expected ID]` |
| 更新工作副本 | `scion update [REV]` |
| 合并历史 | `scion merge THEIRS [--ours REV] [--base REV] -m MESSAGE` |
| 重放单个 changeset | `scion rebase ONTO [--revision REV]` |
| 查看历史 | `scion log [REV] [--path PATH] [--limit N]` · `scion show [REV]` |
| 查看行来源或搜索文本 | `scion blame PATH [--rev REV]` · `scion search QUERY [--prefix PATH]` |
| 列出共享 ref | `scion refs` |
| 导入 Git 历史 | `scion import-git PATH --server URL [--ref main] [--revision HEAD]` |
| 校验服务端存储 | `scion fsck --data DIRECTORY` |
| 创建仓库快照 | `scion backup --data DIRECTORY --output NEW_DIRECTORY` |
| 恢复经过校验的快照 | `scion restore SNAPSHOT --data NEW_DIRECTORY` |
| 查看 ref 审计历史 | `scion ref-log [--name main] [--limit N]` |
| 恢复中断的更新 | `scion recover` |

`land --expected none` 要求目标 ref 尚不存在。如果不传 `--expected`，客户端会先读取当前目标，并将这个快照用于比较并交换。Merge 和 rebase 创建 changeset 后更新工作区，不会推进共享 ref。

Git 导入将可达提交 DAG 转成 Scion changeset，在每条提交信息末尾添加 `Imported-from-git` 来源记录，并返回原始与导入后的 tip ID。目标 ref 必须尚未使用。两套 ID 不同。支持普通文件和可执行文件；拒绝符号链接和子模块。原生操作无需 Git。

工作区采用可跨平台使用的路径名，拒绝保留设备名、冒号、结尾的点或空格，以及大小写或 Unicode 归一化后的路径冲突。Windows 上提交时保留已跟踪文件的可执行模式；新文件默认不可执行。

## 可靠的工作流

回归测试覆盖不可变对象校验、竞争 ref 更新、分叉合并、稀疏提交、工作副本冲突保护、缓存损坏，以及 CLI 到 HTTP 的完整流程。

| 行为 | 实现方式 |
| --- | --- |
| 共享 ref 更新 | 精确的原子比较并交换；持有旧目标的写入会失败，不会覆盖更新后的目标。 |
| 合并冲突 | 返回明确的冲突路径供处理，共享 ref 保持原值。 |
| 工作副本更新 | 拒绝更新存在修改或删除的已跟踪文件；保护发生路径冲突的未跟踪和已忽略文件。无关的未跟踪文件会保留。 |
| 内容完整性 | 服务端读对象、客户端下载时校验带类型信息的 SHA-256 标识；`fsck` 检查服务端一致性。 |
| 稀疏提交 | 仅提交已落盘范围内的增量，从父版本保留未检出的路径。 |

## 部署

通过 `scion serve --data DIRECTORY` 启动一个仓库服务。仓库元数据存储在 SQLite WAL 中，不可变 blob 和树节点压缩后保存在同一数据目录下。SQLite 事务协调并发写入；每个开发者或 agent 使用独立工作区。

默认监听地址为 `127.0.0.1:7433`。共享仓库时，在服务端和客户端配置相同的 `SCION_TOKEN` 环境变量，并通过 HTTPS 反向代理或 SSH 隧道提供远程传输。Token 授予整个仓库的访问权，监听非回环地址必须配置 token。客户端从环境中读取 token，不将其写入工作区元数据，并拒绝 HTTP 重定向。

运行 `scion fsck --data DIRECTORY` 可以检查仓库一致性。[存储与并发](docs/ARCHITECTURE.md#concurrency-and-refs)、[工作副本更新](docs/ARCHITECTURE.md#update-protection)和 [HTTP 传输参考](docs/API.md#transport-and-authentication)介绍了具体运维方式。

服务提供 `/ready` 仓库就绪检查，以及需要认证的 `/v1/metrics` 运行指标。通过
`--max-workers`、`--max-inflight-bytes`、`--request-timeout` 配置请求准入，使用
`--access-log` 输出 JSON 访问日志，并通过 `X-Request-ID` 关联请求。

仓库提供 [Dockerfile](Dockerfile) 和 [Compose 配置](compose.yaml)，用于运行带持久化卷的容器服务。
[运维指南](docs/OPERATIONS.md)说明部署、在线快照、校验恢复、审计历史和工作区恢复流程。

## 技术参考

| 项目 | 规格 |
| --- | --- |
| 本地 blob 缓存 | 64 MiB，校验内容完整性，按最近使用情况淘汰。 |
| HTTP 请求体 | 最大 16 MiB，包含原始 blob 上传。 |
| 服务准入 | 默认最多 32 个并发 worker、64 MiB 已接纳请求体，两个上限可分别配置。 |
| 图查询 | 按提交代数剪枝，复用最多 4,096 条不可变元数据缓存。 |
| 原生文件模式 | 普通文件（`100644`）和可执行文件（`100755`）；保留二进制内容。 |
| 服务端文本检查 | 不超过 1 MiB、20,000 行的 UTF-8 文件；重叠的二进制修改返回冲突。 |
| 历史与搜索 | 沿第一父版本执行 log/blame，对选定文件进行字面子串搜索。 |
| Rebase | 将一个 changeset 相对第一父版本的增量重放到指定版本。 |
| 更新流程 | OS 进程锁、持久化暂存文件与日志、原子元数据提交，以及离线恢复。 |

[架构指南](docs/ARCHITECTURE.md)介绍算法与存储格式；[API 参考](docs/API.md)介绍请求字段、响应结构、认证和错误处理。

## 开发

开发工作按三个 [milestones](https://github.com/rizaust/open-scion/milestones) 组织：

| 里程碑 | 重点 |
| --- | --- |
| [v0.2 — Reliable core](https://github.com/rizaust/open-scion/milestone/1) | 模块集成、持久化工作区恢复，以及仓库备份运维。 |
| [v0.3 — Team collaboration](https://github.com/rizaust/open-scion/milestone/2) | 带权限范围的身份、幂等请求和受保护 ref 的落地策略。 |
| [v0.4 — Storage and query scale](https://github.com/rizaust/open-scion/milestone/3) | 存储后端、批量对象传输、索引、垃圾回收和共享缓存。 |

每个 issue 都包含具体范围与验收条件，可以通过 milestone 查看实现和集成进度。

```sh
python -m unittest discover -s tests -v
python -m compileall -q open_scion
```

可从[贡献指南](CONTRIBUTING.md)、[架构说明](docs/ARCHITECTURE.md)和 [API 参考](docs/API.md)开始。欢迎通过 [GitHub issues](https://github.com/rizaust/open-scion/issues)提交可复现问题或讨论改进。

采用 [Apache License 2.0](LICENSE) 许可证。
