# multi-claude：PyPI、Homebrew 与 README 首页（v0.4.0-B）

> 2026-10-02 注记：代码已按方案实现（pyproject 发行名 `multi-claude-cli`、`.github/workflows/pypi.yml`、`.gitattributes`、`scripts/make-assets.py` 与 `docs/assets/`、README 双语首页/分步快速开始/FAQ、CONTRIBUTING、CHANGELOG、`tests/test_packaging.py`），独立代码评审可合并，本机与编译机 Python 3.8.10 全量通过；T6（Homebrew）、T7（PyPI）在 v0.4.0 发布后做。

## 1. 背景

- PyPI 上 `multi-claude` 这个名字已被另一个项目占用（2026-10-02 查询 `https://pypi.org/pypi/multi-claude/json`：1.0.31 版，主页 `github.com/ghackk/claude-multi-account`）。`multi-claude-cli` 当时未被注册（HTTP 404）。
- 目前只能 `curl | sh` 或 `pipx install git+…` 安装（`README.md:33-57`）。
- README 开头只有一段文字和一个示意代码块（`README.md:1-15`），没有徽章、演示图，也没有常见问题。
- Homebrew tap 仓库 `jakoes-wu/homebrew-tap` 已存在，目前只有 `Formula/multi-codex.rb`。

## 2. 目标 / 非目标

**目标**

1. 发布 release 时，GitHub Actions 构建 sdist 与 wheel，并通过 PyPI Trusted Publishing 上传，发布名为 `multi-claude-cli`；也能对已有 tag 手动补发。
2. 在 `jakoes-wu/homebrew-tap` 增加 `Formula/multi-claude.rb`，安装 release 附件里的源码包（v0.4.0-A 产出）。
3. README（中英）首页：
   - 徽章、演示 GIF、“How it works”；
   - 分步快速开始；
   - 常见问题；
   - 安装一节补上 PyPI 与 Homebrew 两种方式。
4. 用脚本生成演示 GIF 和 1280×640 的社交预览图。

**非目标**

- 不改任何命令行为，不改 `install.sh`。
- 不在 release 工作流里自动更新 tap 配方：需要跨仓库写权限的令牌。改为在 CONTRIBUTING 写明手动更新步骤。
- 不提交到 homebrew-core。
- PyPI 账号、Trusted Publisher（pending publisher）的配置，以及在仓库设置里上传社交预览图，由用户完成（GitHub API 不支持上传社交预览图）。

## 3. 假设与约束

- **PyPI 侧前置**：用户在 PyPI 为项目 `multi-claude-cli` 添加 pending publisher（仓库 `jakoes-wu/multi-claude`、工作流 `pypi.yml`、环境 `pypi`）。未配置时，工作流的上传步骤失败，构建与检查步骤不受影响。
- **仓库侧前置**：在 GitHub 仓库建环境 `pypi`。可以用 `gh api -X PUT repos/jakoes-wu/multi-claude/environments/pypi` 创建，执行前征得用户同意。
- **改发布名的影响**：`pyproject.toml` 的 `name` 改为 `multi-claude-cli`，import 包仍是 `multi_claude`，命令入口仍是 `multi-claude`。wheel 文件名变为 `multi_claude_cli-<版本>-py3-none-any.whl`。
- **演示素材**：用 Pillow 按真实命令的输出逐帧绘制（本机 Pillow 12.0.0、`/System/Library/Fonts/Menlo.ttc` 与 `Helvetica.ttc` 已核实存在）。命令在临时 HOME 中实际运行，配合假 `claude`、假 `security`（返回 0，让 LOGIN 显示 keychain）和假的 `.claude.json` 用量缓存；账号名用 `work`、`personal`，GIF 下方注明是示例。
- **图片地址与打包**：README 中的图片用 `raw.githubusercontent.com` 的绝对地址，PyPI 项目页才能显示。`.gitattributes` 写入 `docs/assets export-ignore`，图片不进 release 源码包。
- **README 内容的出处**：常见问题只写仓库里已实现、已验证的行为（升级方法、`claude` 用的是哪个账号、`handoff`、`usage` 的数据来源、共享、凭据边界）。VS Code 相关问题等 v0.6.0 实测后再写。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `109ebf4`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `pyproject.toml` | `[project]` :5-18、`[project.urls]` :23-24 | 修改 | `name = "multi-claude-cli"`；补 `keywords`、Python 版本 classifier、`Intended Audience`、`Topic :: Utilities`；urls 补 Changelog、Issues |
| `.github/workflows/pypi.yml` | 新文件 | 新增 | §5.1.1 |
| `scripts/make-assets.py` | 新文件 | 新增 | §5.1.3 |
| `docs/assets/demo.gif`、`docs/assets/social-preview.png` | 新文件 | 新增 | 由脚本生成 |
| `.gitattributes` | 新文件 | 新增 | `docs/assets export-ignore` |
| `README.md`、`README.zh-CN.md` | 开头 :1-15、Features :16、Installation :33-57、Quick start :59-70、Reference 之前 | 修改 / 新增 | §5.1.4 |
| `CONTRIBUTING.md` | Releasing :37-50 | 修改 | 补 PyPI 与 tap 配方更新步骤 |
| `CHANGELOG.md` | Unreleased | 修改 | Added 条目 |
| `tests/test_packaging.py` | 新文件 | 新增 | §8 T1–T3 |
| tap 仓库 `jakoes-wu/homebrew-tap` 的 `Formula/multi-claude.rb` | 新文件（另一个仓库） | 新增 | §5.1.2；v0.4.0 发布、附件就绪后提交，提交前征得用户同意 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 `pypi.yml`

照搬 multi-codex 的 `pypi.yml` 结构：

- **触发**：`release: published`，以及带 `tag` 输入的 `workflow_dispatch`。
- **`build` 作业**：
  - checkout 该 tag；
  - `python -m build`；
  - `twine check --strict dist/*`；
  - 核对 `dist/multi_claude_cli-<版本>-py3-none-any.whl` 与 `dist/multi_claude_cli-<版本>.tar.gz` 都存在，版本取 tag 去掉 `v`；
  - 上传 artifact。
- **`publish` 作业**：`environment: pypi`，`permissions: id-token: write`，用 `pypa/gh-action-pypi-publish@release/v1` 上传，不使用 API token。
- tag 只经环境变量引用，不在 `run` 中内联 `${{ }}`。

#### 5.1.2 Homebrew 配方 `Formula/multi-claude.rb`

与 multi-codex 的配方相同的写法：

- `url`：`.../releases/download/v0.4.0/multi-claude-v0.4.0.tar.gz`；`sha256` 取该 release 的 `SHA256SUMS`。
- `depends_on "python@3.13"`；`libexec.install "src/multi_claude"`。
- `bin/"multi-claude"` 写包装脚本：用 `PYTHONPATH` 指向 `libexec`，再用该 Python 执行 `-m multi_claude "$@"`。
- `test do`：断言 `--version` 含版本号、`multi-claude`（无参数，HOME 为 `testpath`）输出含 `Get started`。
- **本机验证**：在临时本地 tap 中 `brew install --build-from-source`、`brew test`、`brew audit --strict --new`，最后卸载并删除临时 tap，避免与 `~/.local/bin/multi-claude` 并存。

#### 5.1.3 `scripts/make-assets.py`

头部注释写清前置条件，支持 `-h`。

1. 在临时目录建 HOME，PATH 里放假 `claude` 与假 `security`；依次实际运行：
   - `multi-claude add work --proxy 7901`
   - `multi-claude add personal`
   - 给两个账号写入假的 `.claude.json` 用量缓存（`fetchedAtMs` 为当前时间）
   - `multi-claude`（无参数，显示简表）

   抓取它们的真实输出。
2. 用 Pillow 按终端样式逐帧绘制：命令逐字出现，输出整段出现；字体固定用 Menlo，找不到就报错退出，不回落到其它字体。
3. 输出 `docs/assets/demo.gif`（≤ 1 MB）与 `docs/assets/social-preview.png`（1280×640，含项目名、一句话说明和一段简表输出）。
4. 结束时删除临时目录。不碰真实的 `~/.claude`、`~/.cc`。

#### 5.1.4 README（中英结构一致）

1. **开头**：
   - 标题、语言切换；
   - 徽章：Release、CI、PyPI、Python 3.8+、License。PyPI 徽章等首次上传成功后再加，避免 README 出现装不上的包；
   - 一句话说明；
   - 三行示例：`claude-work`、`claude-personal`、`multi-claude`；
   - 演示 GIF 与“示例账号”小字。
2. **How it works**：沿用现有的“每个账号一个目录 + 启动命令”说明与示意块（`README.md:7-14`），并移到这一节下。
3. **Installation**：
   - 首推 `curl | sh`；
   - 补 `pipx install multi-claude-cli`（首次上传 PyPI 成功后写入）；
   - 补 `brew install jakoes-wu/tap/multi-claude`（配方提交并验证后写入）。
4. **Quick start 改为分步**：
   1. 每个登录建一个账号（`add`）；
   2. 每个账号登录一次（`login`）；
   3. 用启动命令代替 `claude`；
   4. 用 `multi-claude` / `doctor` 检查。

   另加一小节“已经在用 Claude Code？”，说明 `~/.claude` 不受影响、迁移可选。
5. **FAQ（常见问题，`## Reference` 之前）**，每条回答指向对应命令或章节：
   - 怎么升级？
   - 直接运行 `claude` 用的是哪个账号？
   - 怎么把对话交给另一个账号？
   - 为什么 `usage` 的数字是旧的？
   - 怎么让所有账号共用 skills 和 CLAUDE.md？
   - multi-claude 会读取或复制我的登录凭据吗？

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| PyPI 包 `multi-claude-cli` | 新增分发渠道 | 新增 |
| `pyproject.toml` `name` | `multi-claude` → `multi-claude-cli` | 已用 `pipx install git+…` 装过的用户，pipx 里的包名会变；重新执行 `pipx install multi-claude-cli`（或 `--force`）即可。README 写明 |
| Homebrew 配方 | 新增（另一个仓库） | 新增 |
| 工作流 `pypi.yml`、环境 `pypi` | 新增 | 新增 |
| 命令行、配置、`install.sh` | 无变化 | — |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **PyPI 用 `multiclaude` 等名字**：用户已选定 `multi-claude-cli`。
- **用 vhs / asciinema 录制演示**：本机没有这两个工具；用脚本按真实输出绘制，结果可重复生成，因此采用脚本。

## 7. 影响分析

- **发布流程**：多一个工作流。它失败（PyPI 未配置）不影响 release 和 `install.sh`。
- **源码包体积**：图片被 `export-ignore` 排除，不变。
- **README 结构**：在第二批重排的基础上加开头、FAQ，并把快速开始改为分步。`test_help.py` T10 断言中英两版标题结构一致、关键标题存在，改完要同步通过。
- **测试**：`pyproject.toml` 改名不影响以 `python -m multi_claude` 运行的测试；新增 `test_packaging.py` 用于核对元数据与 README 图片地址。

## 8. 回归测试

**环境**：本机与编译机 ubuntu20（Python 3.8.10）各跑一遍 `python3 -m unittest discover -s tests`。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | `pyproject.toml` | `name` 为 `multi-claude-cli`；入口 `multi-claude = multi_claude.cli:main`；urls 含 Changelog、Issues |
| T2 | 本机 `python -m build` + `twine check --strict` | 生成 `multi_claude_cli-<版本>` 的 wheel 与 sdist，检查通过；wheel 中含 `multi_claude/cli.py` |
| T3 | README 图片与链接 | 两版都引用 `raw.githubusercontent.com/jakoes-wu/multi-claude/main/docs/assets/demo.gif`，且文件存在；标题结构一致（`test_help` T10） |
| T4 | `scripts/make-assets.py` | 运行后生成两个文件，GIF ≤ 1 MB、PNG 为 1280×640；真实的 `~/.cc` 与 `~/.claude` 未被改动 |
| T5 | `.gitattributes` | `git archive HEAD` 生成的包里没有 `docs/assets` |
| T6 | Homebrew 配方（v0.4.0 发布后） | 本地临时 tap 中 `brew install --build-from-source`、`brew test`、`brew audit --strict --new` 通过 |
| T7 | PyPI（v0.4.0 发布后，需用户先配置 Trusted Publisher） | 工作流成功；`pipx install multi-claude-cli` 后 `multi-claude --version` 为 0.4.0 |
| T8 | 回归 | 现有全部用例通过 |

## 9. 日志 / 观测点

- `pypi.yml` 日志：`twine check` 结果、版本核对失败时的 `no wheel for <版本> in dist/`。
- PyPI 项目页 `https://pypi.org/project/multi-claude-cli/`。
- `brew test multi-claude` 的输出。
