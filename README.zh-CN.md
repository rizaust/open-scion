<p align="center"><img src="docs/assets/cover.svg" alt="Open Scion：面向 agent 时代的版本控制" width="100%"></p>

# Open Scion

[English](README.md) · **简体中文**

**Open Scion 是对 Anthropic Scion 已公布核心架构的独立实现。**

这是一个面向开发者、编程 agent 和 CI 的服务端版本控制工具：服务端保存不可变 changeset，客户端按需获取文件，创建提交与推进共享分支是两个独立操作。

架构参考来自 Anthropic 于 2026 年 10 月 13 日发布的技术文章 [Redesigning source control at Anthropic](https://www.anthropic.com/engineering/redesigning-source-control)。本项目独立开发，与 Anthropic 无隶属关系，未经其背书，不包含其内部实现代码，也不宣称与其内部协议兼容。

**开发状态：**首版正在实现中，面向单服务器上的可信团队，采用 Python 标准库、SQLite WAL 和压缩内容寻址对象。分布式存储与大规模生产性能不属于首版承诺。完整使用说明、架构图和验证结果将随实现补齐。
