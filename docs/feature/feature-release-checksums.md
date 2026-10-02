# multi-claude：发布包与安装校验（v0.4.0-A）

> 2026-10-02 注记：代码已按方案实现（`.github/workflows/release.yml`、`install.sh` 下载段，移植自 multi-codex 并替换名称），本机安装测试 20 个通过，待独立代码评审与编译机验证，未提交。基线：main `d019ec1`（v0.3.1）。来源：用户 2026-10-02 同意的迭代计划（v0.4.0 分发与首页），参考同作者项目 multi-codex 的同名方案与实现（提交 `748177f`）。本方案是 v0.4.0 的第一部分；PyPI、Homebrew 与 README 首页见 `feature-distribution.md`（第二部分，依赖本方案产出的 release 附件）。

## 1. 背景

- `install.sh` 安装 release 时，先读 `releases/latest` 得到 tag，再下载 `codeload.github.com/<repo>/tar.gz/<tag>`（`install.sh:137-151`），不做任何完整性校验。脚本里的 SHA-256（`tree_hash`）只用于比较“已安装的包与新包是否相同”，不是下载校验。
- GitHub 自动生成的源码包不保证字节稳定（同一 tag 在不同时间下载，压缩结果可能不同），不能拿来发布校验和。
- 仓库目前只有 `ci.yml` 一个工作流。

## 2. 目标 / 非目标

**目标**

1. 发布 release 时，GitHub Actions 生成固定的 `multi-claude-<tag>.tar.gz` 和 `SHA256SUMS`，作为 release 附件上传，并自检包内版本号与 tag 一致。
2. `install.sh` 安装 release 时，优先下载这个附件，按 `SHA256SUMS` 校验，不一致就停止，退出码 1，不改动已安装的版本。
3. 没有附件时（v0.3.1 及更早的 release、或安装分支），退回原来的下载方式，并明确输出“没有校验”；设置 `MULTI_CLAUDE_REQUIRE_CHECKSUM=1` 时改为报错退出。

**非目标**

- 不做签名或构建来源证明（attestation）。校验和与包在同一个 release 里，只能发现下载过程中的损坏和替换，不能防范 GitHub 账号本身被攻破；README 如实写明。
- 不为已发布的 v0.1.0–v0.3.1 补传附件。
- 不改变从克隆目录运行 `./install.sh`（本地安装）的行为。

## 3. 假设与约束

- `release` 事件只在用户凭据（不是 `GITHUB_TOKEN`）创建 release 时触发；本项目一直用 `gh release create` 手工发布，满足这个条件（multi-codex 同样做法，已在其 v0.5.0–v0.7.0 实际运行）。
- release JSON 的 `assets[].name`、`assets[].browser_download_url` 字段来自 GitHub REST API（`GET /repos/{owner}/{repo}/releases/latest` 与 `/releases/tags/{tag}`）。
- `install.sh` 里读 JSON、算哈希都用已确认可用的 `$PYTHON`（3.8+ 标准库），不依赖 `jq`、`sha256sum`、`shasum`。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `d019ec1`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| 发布工作流 | 新文件 `.github/workflows/release.yml` | 新增 | §5.1.1 |
| `install.sh` | 用法说明 Environment :39-44 | 修改 | 新增 `MULTI_CLAUDE_SHA256`、`MULTI_CLAUDE_REQUIRE_CHECKSUM`、`MULTI_CLAUDE_CODELOAD` 三项说明 |
| `install.sh` | 下载段 :137-152 | 修改 | §5.1.2 的选择与校验逻辑；读 release JSON 改用 python 的 `json`（替换 :143 的 sed） |
| `tests/test_install.py` | `make_tarball` 一带 | 修改 / 新增 | 构造离线的假 release JSON、附件与 `SHA256SUMS`；见 §8 |
| `README.md`、`README.zh-CN.md` | Installation 一节（:33-55） | 修改 | 说明安装 release 时会校验、`MULTI_CLAUDE_REQUIRE_CHECKSUM` 与安全边界 |
| `CONTRIBUTING.md` | 末尾 | 新增 | “Releasing”一节：改版本号与 CHANGELOG → PR → `gh release create` → 确认工作流上传了附件 |
| `CHANGELOG.md` | Unreleased | 修改 | Added 条目 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 `release.yml`

- 触发：`on: release: types: [published]`；权限：`contents: write`。
- 环境：`GH_TOKEN: ${{ github.token }}`、`TAG: ${{ github.event.release.tag_name }}`。脚本里只引用 `$TAG`，不在 `run` 中内联 `${{ }}` 表达式，避免脚本注入。
- 步骤：
  1. `actions/checkout@v4`，`ref` 为该 tag。
  2. `git archive --format=tar.gz --prefix="multi-claude-${TAG}/" -o "multi-claude-${TAG}.tar.gz" HEAD`，再 `sha256sum … > SHA256SUMS`。
  3. 自检：解包后确认有 `src/multi_claude/__init__.py`，且其中的 `__version__` 等于去掉前缀 `v` 的 tag；不一致则让工作流失败。
  4. `gh release upload "${TAG}" "multi-claude-${TAG}.tar.gz" SHA256SUMS --clobber --repo "${GITHUB_REPOSITORY}"`。

#### 5.1.2 `install.sh` 的下载与校验

新增变量：

- `EXPECTED_SHA="${MULTI_CLAUDE_SHA256:-}"`
- `API="${MULTI_CLAUDE_API:-https://api.github.com}"`
- `CODELOAD="${MULTI_CLAUDE_CODELOAD:-https://codeload.github.com}"`（供测试离线使用）

按下表决定下载什么、怎么校验：

| 情况 | 下载 | 校验 |
| ---- | ---- | ---- |
| 设置了 `MULTI_CLAUDE_TARBALL` | 该地址 | 有 `MULTI_CLAUDE_SHA256` 就按它校验，否则不校验 |
| 没有设置 `MULTI_CLAUDE_REF` | 读 `releases/latest`（读不到时保留现有报错文字，`test_no_release_without_ref` 依赖它） | 见下两行 |
| release 里有 `multi-claude-<tag>.tar.gz` 与 `SHA256SUMS` 两个附件 | 附件包 | 从 `SHA256SUMS` 中取该文件名对应的值；找不到该文件名时报错退出 |
| release 里没有这两个附件 | `${CODELOAD}/<repo>/tar.gz/<tag>` | 不校验 |
| `MULTI_CLAUDE_REF` 形如版本 tag（完整匹配 `v<数字>.<数字>.<数字>`，可带 `-` / `+` 开头的后缀，由 python 正则判断） | 读 `releases/tags/<REF>`，读到就按上面两行处理；**读不到就报错退出**，不悄悄退回到不校验的下载 | 同上 |
| `MULTI_CLAUDE_REF` 是分支等其它名字 | codeload 源码包 | 不校验 |

- **`SHA256SUMS` 的格式**：每行 `<值>  <文件名>`，也接受二进制模式的 `<值> *<文件名>`；比较时不区分大小写。
- **校验在解包之前完成**：
  - 一致时输出 `verified sha256 <值>`；
  - 不一致时报 `checksum mismatch for <地址>: expected <值>, got <值>`，退出 1，不解包，也不碰已安装的版本。
- **没有校验时**：
  - 默认输出 `note: <地址> is not verified (no checksum published)`；
  - 设置了 `MULTI_CLAUDE_REQUIRE_CHECKSUM=1` 时改为报错退出 1。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| release 附件 | 新增 `multi-claude-<tag>.tar.gz`、`SHA256SUMS`（v0.4.0 起） | 新增 |
| `install.sh` 环境变量 | 新增 `MULTI_CLAUDE_SHA256`、`MULTI_CLAUDE_REQUIRE_CHECKSUM`、`MULTI_CLAUDE_CODELOAD` | 新增 |
| `install.sh` 安装旧 release | 仍走 codeload，多一行“未校验”提示 | 兼容 |
| `install.sh` 指定版本 tag 而 API 读不到 | 由“退回 codeload 下载”改为报错 | 收紧：避免在无法校验时悄悄降级；错误信息提示可改用 `MULTI_CLAUDE_TARBALL` |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **对 codeload 源码包计算校验和**：GitHub 不保证该包字节稳定，同一 tag 会出现不同哈希，因此不采用。
- **用 `sha256sum` / `shasum` 计算**：两个平台的命令不同，脚本已经依赖 python3，统一用 python 计算。

## 7. 影响分析

- **新安装与升级**：从 v0.4.0 起，`curl | sh` 默认下载附件包并校验，比现在多读一次 `SHA256SUMS`（几十字节）。
- **工作流**：只在发布 release 时运行，不影响 CI；它失败（例如忘了改版本号）时 release 没有附件，`install.sh` 退回 codeload 并提示“未校验”，安装仍可用。
- **Homebrew 配方**（第二部分）将使用这个附件包与 `SHA256SUMS`。
- **测试**：现有远程安装用例走 `MULTI_CLAUDE_TARBALL`（`file://`），不受选择逻辑影响；新用例通过 `file://` 构造假的 API 与附件，离线运行。

## 8. 回归测试

**环境**：本机与编译机 ubuntu20（Python 3.8.10）各跑一遍 `python3 -m unittest discover -s tests`；`shellcheck install.sh`。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 假 `releases/latest` 带附件和正确的 `SHA256SUMS` | 安装成功，输出 `verified sha256` |
| T2 | `SHA256SUMS` 中的值被改错 | 退出 1，报 `checksum mismatch`；已安装的版本不变 |
| T3 | `SHA256SUMS` 中没有该文件名 | 退出 1，报 `has no entry for` |
| T4 | release 没有附件 | 退回 `MULTI_CLAUDE_CODELOAD` 下载，输出 `is not verified`；加 `MULTI_CLAUDE_REQUIRE_CHECKSUM=1` 时退出 1 |
| T5 | `MULTI_CLAUDE_REF=v9.9.9` 而 API 读不到 | 退出 1，报 `cannot read release` 并提示 `MULTI_CLAUDE_TARBALL` |
| T6 | `MULTI_CLAUDE_REF=main` | 走 codeload，提示未校验，不读 API |
| T7 | `MULTI_CLAUDE_TARBALL` 加正确 / 错误的 `MULTI_CLAUDE_SHA256` | 分别成功 / 退出 1 |
| T8 | `SHA256SUMS` 为二进制模式（`<值> *<文件名>`）且大写十六进制 | 能识别并校验通过 |
| T9 | 回归 | 现有全部用例通过（基线 294 个）；`shellcheck` 无告警 |
| T10 | 发布后（L3） | 发布 v0.4.0 时工作流上传两个附件；从 release 运行 `curl … | sh` 输出 `verified sha256` |

## 9. 日志 / 观测点

- `[multi-claude-install] verified sha256 <值>`
- `[multi-claude-install] note: <地址> is not verified (no checksum published)`
- `[multi-claude-install] error: checksum mismatch for …` / `… has no entry for …` / `cannot read release …`
- 工作流日志：`cat SHA256SUMS` 的输出；版本不一致时的 `__version__ … does not match tag …`
