<p align="center"><img src="docs/assets/cover.svg" alt="Open Scion: source control for an agent-filled world" width="100%"></p>

<p align="center">
  <a href="https://github.com/rizaust/open-scion/actions/workflows/ci.yml"><img src="https://github.com/rizaust/open-scion/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB" alt="Python 3.11 or newer">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-green" alt="Apache 2.0 license"></a>
</p>

# Open Scion

**English** · [简体中文](README.zh-CN.md)

**An independent implementation of the core architecture published for Anthropic Scion.**

Open Scion is a server-first source control tool for humans, coding agents, and CI.
Immutable changesets live on the server. Clients request the files they need.
Publishing work and advancing a shared branch are separate operations.

The architectural reference is Anthropic's October 13, 2026 engineering article,
[Redesigning source control at Anthropic](https://www.anthropic.com/engineering/redesigning-source-control).
This project is independently developed and is not affiliated with, endorsed by,
or source-compatible with Anthropic. It contains no Anthropic implementation code.

**Development status:** initial implementation in progress. The first release targets
a single trusted team on one server, using Python's standard library, SQLite WAL,
and compressed content-addressed objects. Distributed storage and production-scale
performance are outside the initial release. Runnable instructions and validation
results will be added alongside the implementation.
